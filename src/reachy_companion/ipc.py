"""Authenticated Unix RPC. Disconnect cancels handlers; calls are never replayed."""

import asyncio
import base64
import contextlib
import ctypes
import fcntl
import os
import socket
import stat
import struct
import time
import uuid
from collections import OrderedDict
from pathlib import Path

from .protocol import CLOUD_LIMIT, CHUNK, LINE_LIMIT, VERSION, Fault, Transfers, decode, encoded, line, request


def peer_uid(sock):
    if hasattr(socket, "SO_PEERCRED"):
        return struct.unpack("3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[1]
    # Darwin/BSD's getpeereid is not exposed by all Python builds.
    uid, gid = ctypes.c_uint(), ctypes.c_uint()
    libc = ctypes.CDLL(None, use_errno=True)
    if not hasattr(libc, "getpeereid") or libc.getpeereid(sock.fileno(), ctypes.byref(uid), ctypes.byref(gid)) != 0:
        raise Fault("not_connected", "peer_identity")
    return uid.value


class SocketLease:
    """Lock before removing a stale socket. Parent must be owned and not writable by peers."""
    def __init__(self, path, gid=None):
        self.path, self.gid, self.fd = Path(path), gid, None

    def acquire(self):
        parent = self.path.parent
        parent.mkdir(mode=0o750, parents=True, exist_ok=True)
        info = parent.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_mode & 0o022 or parent.resolve() != parent.absolute()):
            raise Fault("invalid_params", "socket_directory")
        for ancestor in parent.resolve().parents:
            info = ancestor.stat()
            if (info.st_uid not in (0, os.geteuid())
                    or info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX):
                raise Fault("invalid_params", "socket_ancestor")
        if self.gid is not None and os.geteuid() == 0:
            os.chown(parent, os.geteuid(), self.gid)
        self.fd = os.open(str(self.path) + ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(self.fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
                raise Fault("invalid_params", "socket_lock")
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                info = self.path.lstat()
            except FileNotFoundError:
                return
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.geteuid():
                raise Fault("invalid_params", "socket_path")
            self.path.unlink()
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


class Server:
    def __init__(self, path, handler, *, allowed_uids, gid=None, on_disconnect=None):
        self.path, self.handler, self.allowed_uids = Path(path), handler, set(allowed_uids)
        self.lease = SocketLease(path, gid)
        self.transfers = Transfers()
        self.connections = set()
        self.server = None
        self.on_disconnect = on_disconnect
        self.sweeper = None

    async def start(self):
        if self.lease.fd is None:
            self.lease.acquire()
        try:
            self.server = await asyncio.start_unix_server(self._accept, str(self.path), limit=LINE_LIMIT)
            os.chmod(self.path, 0o660)
            if self.lease.gid is not None and os.geteuid() == 0:
                os.chown(self.path, os.geteuid(), self.lease.gid)
            self.inode = self.path.stat().st_ino
            self.sweeper = asyncio.create_task(self._sweep())
        except BaseException:
            self.lease.close()
            raise
        return self

    async def _sweep(self):
        while True:
            await asyncio.sleep(1)
            self.transfers.purge()

    async def close(self):
        if self.sweeper:
            self.sweeper.cancel()
            await asyncio.gather(self.sweeper, return_exceptions=True)
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        for task in list(self.connections):
            task.cancel()
        await asyncio.gather(*self.connections, return_exceptions=True)
        if self.server and self.path.exists() and self.path.lstat().st_ino == self.inode:
            self.path.unlink()
        self.lease.close()

    async def _accept(self, reader, writer):
        current = asyncio.current_task()
        self.connections.add(current)
        owner, pending, seen = object(), set(), OrderedDict()
        write_lock = asyncio.Lock()

        async def respond(value):
            async with write_lock:
                writer.write(line(value))
                await asyncio.wait_for(writer.drain(), 5)

        async def dispatch(value):
            ident = value.get("id")
            try:
                remaining = request(value)
                if ident in seen:
                    raise Fault("outcome_unknown", "duplicate_request")
                seen[ident] = None
                if len(seen) > 256:
                    seen.popitem(last=False)
                async with asyncio.timeout(remaining):
                    if value["op"].startswith("transfer."):
                        result = self.transfers.dispatch(owner, value["op"], value["payload"])
                    else:
                        result = await self.handler(value["op"], value["payload"], owner, value["deadline"])
                answer = {"v": VERSION, "id": ident, "result": result}
                if len(encoded(answer)) + 1 > LINE_LIMIT:
                    answer["result"] = {"$transfer": self.transfers.put(owner, "tool-result", encoded(result))}
                line(answer)
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                answer = {"v": VERSION, "id": ident, "error": Fault("timeout").as_dict()}
            except Fault as exc:
                answer = {"v": VERSION, "id": ident, "error": exc.as_dict()}
            except Exception:
                answer = {"v": VERSION, "id": ident, "error": Fault("internal_error").as_dict()}
            try:
                await respond(answer)
            except (OSError, TimeoutError, Fault):
                writer.close()

        try:
            if len(self.connections) > 16 or peer_uid(writer.get_extra_info("socket")) not in self.allowed_uids:
                return
            while True:
                raw = await asyncio.wait_for(reader.readline(), 150)
                if not raw:
                    break
                value = decode(raw)
                if len(pending) >= 16:
                    break
                task = asyncio.create_task(dispatch(value))
                pending.add(task)
                task.add_done_callback(pending.discard)
        except (OSError, ValueError, TimeoutError, Fault):
            pass
        finally:
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            self.transfers.discard(owner)
            if self.on_disconnect:
                await self.on_disconnect(owner)
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()
            self.connections.discard(current)


class Client:
    def __init__(self, path, uid):
        self.path, self.uid = path, uid
        self.pending = {}
        self.writer = None
        self.read_task = None
        self.write_lock = asyncio.Lock()

    async def __aenter__(self):
        try:
            self.reader, self.writer = await asyncio.wait_for(asyncio.open_unix_connection(str(self.path), limit=LINE_LIMIT), 3)
            if peer_uid(self.writer.get_extra_info("socket")) != self.uid:
                raise Fault("not_connected", "peer_identity")
        except (OSError, TimeoutError):
            raise Fault("worker_unavailable", "connect") from None
        except BaseException:
            await self.close()
            raise
        self.read_task = asyncio.create_task(self._read())
        return self

    async def __aexit__(self, *args):
        await self.close()

    async def close(self):
        if self.writer:
            self.writer.close()
            with contextlib.suppress(OSError):
                await self.writer.wait_closed()
        if self.read_task:
            self.read_task.cancel()
            await asyncio.gather(self.read_task, return_exceptions=True)
        for future in self.pending.values():
            if not future.done():
                future.set_exception(Fault("outcome_unknown", "disconnect"))

    async def _read(self):
        try:
            while raw := await self.reader.readline():
                value = decode(raw)
                future = self.pending.get(value.get("id"))
                if future is not None and not future.done():
                    if value.get("v") != VERSION:
                        raise Fault("invalid_params", "protocol_version")
                    if "error" in value:
                        err = value["error"]
                        future.set_exception(Fault(err["code"], err["stage"]))
                    else:
                        future.set_result(value["result"])
        except (OSError, ValueError, Fault, KeyError, TypeError):
            pass
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(Fault("outcome_unknown", "disconnect"))

    async def call(self, op, payload=None, *, timeout=10):
        if not self.writer or self.writer.is_closing() or self.read_task.done():
            raise Fault("not_connected", "ipc")
        ident = str(uuid.uuid4())
        future = asyncio.get_running_loop().create_future()
        data = line({"v": VERSION, "id": ident, "op": op, "payload": payload or {},
                     "deadline": time.time() + min(timeout, 175)})
        self.pending[ident] = future
        try:
            async with asyncio.timeout(timeout):
                async with self.write_lock:
                    self.writer.write(data)
                    await self.writer.drain()
                result = await future
                if isinstance(result, dict) and set(result) == {"$transfer"}:
                    result = decode(await self.download(result["$transfer"], limit=CLOUD_LIMIT), limit=CLOUD_LIMIT)
                return result
        except TimeoutError:
            # Closing the channel causes the server to cancel active requests.
            await self.close()
            raise Fault("outcome_unknown", "ipc_timeout") from None
        except asyncio.CancelledError:
            await self.close()
            raise
        except OSError:
            raise Fault("outcome_unknown", "disconnect") from None
        finally:
            self.pending.pop(ident, None)

    async def upload(self, purpose, data):
        from .protocol import TRANSFER_LIMITS
        ref = await self.call("transfer.begin", {"transfer_id": str(uuid.uuid4()), "purpose": purpose,
                                               "type": TRANSFER_LIMITS[purpose][0], "total": len(data)})
        for offset in range(0, len(data), CHUNK):
            await self.call("transfer.chunk", {"transfer_id": ref["transfer_id"], "offset": offset,
                                             "data": base64.b64encode(data[offset:offset + CHUNK]).decode()})
        await self.call("transfer.end", {"transfer_id": ref["transfer_id"]})
        return ref["transfer_id"]

    async def download(self, ref, *, limit):
        total = ref.get("total")
        if type(total) is not int or not 0 < total <= limit:
            raise Fault("size_limit", "download")
        result = bytearray()
        while len(result) < total:
            chunk = await self.call("transfer.read", {"transfer_id": ref["transfer_id"], "offset": len(result)})
            try:
                data = base64.b64decode(chunk["data"], validate=True)
            except (ValueError, KeyError):
                raise Fault("invalid_params", "download") from None
            if not 0 < len(data) <= CHUNK or len(result) + len(data) > total:
                raise Fault("size_limit", "download")
            result.extend(data)
            if chunk.get("offset") != len(result) or chunk.get("eof") != (len(result) == total):
                raise Fault("invalid_params", "download_offset")
        await self.call("transfer.abort", {"transfer_id": ref["transfer_id"]})
        return bytes(result)
