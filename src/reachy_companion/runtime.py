"""Unprivileged module lifecycle, cancellation, arbitration and local RPC."""

import argparse
import asyncio
import contextlib
import importlib
import logging
import os
import signal
import time
from contextlib import asynccontextmanager

from . import __version__
from .config import Config
from .ipc import Client, Server
from .protocol import MP3_LIMIT, Fault, encoded
from .registry import Cancellation, Registry

log = logging.getLogger(__name__)


class Runtime:
    def __init__(self, config, hardware=None):
        self.config, self.registry = config, Registry()
        self._listeners = set()
        if hardware is None:
            from .hardware.reachy import Hardware
            hardware = Hardware(config)
        self.hardware = hardware
        self.modules, self.started, self.tasks, self.invocations = [], [], set(), {}
        self.locks = {"motion": asyncio.Lock(), "media": asyncio.Lock()}
        self.ready, self.state, self.connected = False, "starting", False
        self.turn_task = None
        self.connection_generation = None
        self.stop_lock = asyncio.Lock()
        self.stopping = False
        self.server = Server(config.worker_socket, self.handle,
                             allowed_uids={os.getuid(), config.adapter_uid}, gid=config.socket_gid)

    @property
    def state(self):
        return self._state

    @state.setter
    def state(self, value):
        if getattr(self, "_state", None) == value:
            return
        self._state = value
        for queue in self._listeners:
            if queue.full():
                queue.get_nowait()
            queue.put_nowait({"type": "state", "state": value})

    async def events(self):
        queue = asyncio.Queue(maxsize=32)
        self._listeners.add(queue)
        queue.put_nowait({"type": "state", "state": self.state})
        try:
            while True:
                yield await queue.get()
        finally:
            self._listeners.discard(queue)

    def spawn(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        def finished(task):
            self.tasks.discard(task)
            if not task.cancelled() and task.exception() is not None:
                log.error("background_task_failed")
        task.add_done_callback(finished)
        return task

    async def start(self):
        self.server.lease.acquire()
        try:
            for name in self.config.modules:
                module = importlib.import_module(name)
                module.register(self.registry)
                self.modules.append(module)
            self.registry.freeze()
            for module in self.modules:
                # Include the failing module in cleanup: start may have acquired resources.
                self.started.append(module)
                if hasattr(module, "start"):
                    await module.start(self)
            self.ready = True
            self.state = "offline"
            await self.server.start()
            self.spawn(self._connectivity())
            log.info("worker version=%s catalog=%s ready", __version__, self.registry.hash)
        except BaseException:
            await self.close()
            raise

    async def close(self):
        if self.server.lease.fd is None and not self.started and not self.ready:
            return
        self.ready = False
        await self.stop()
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        while self.started:
            module = self.started.pop()
            if hasattr(module, "stop"):
                try:
                    await module.stop()
                except Exception:
                    log.error("module_stop_failed")
        await self.server.close()
        self.state = "stopping"

    async def _connectivity(self):
        while True:
            previous = self.connection_generation
            try:
                async with Client(self.config.adapter_socket, self.config.adapter_uid) as client:
                    status = await client.call("connection.status", timeout=3)
                    self.connected = status["connected"]
                    self.connection_generation = status["generation"]
            except Fault:
                self.connected = False
            if self.turn_task and (not self.connected or previous is not None and previous != self.connection_generation):
                self.turn_task.cancel()
            if self.state in ("idle", "offline"):
                self.state = "idle" if self.connected else "offline"
            await asyncio.sleep(.5 if self.turn_task else 2)

    async def status(self):
        observed = await self.hardware.status()
        return {"version": __version__, "ready": self.ready, "state": self.state,
                "muse_connected": self.connected, "catalog_hash": self.registry.hash,
                "hardware": observed}

    @asynccontextmanager
    async def _slot(self, kind):
        if kind == "read":
            yield
        else:
            async with self.locks[kind]:
                yield

    async def invoke(self, name, params, timeout_ms=None):
        if name not in self.registry.tools:
            raise Fault("unknown_tool")
        tool = self.registry.tools[name]
        arguments = tool.validate(params)
        if self.stopping and name != "reachy.stop":
            raise Fault("busy", "stopping")
        if not self.ready:
            raise Fault("worker_unavailable", "not_ready")
        if timeout_ms is not None and (type(timeout_ms) is not int or timeout_ms <= 0):
            raise Fault("invalid_params", "timeout")
        budget = min(tool.timeout_ms, timeout_ms or tool.timeout_ms) / 1000
        token, task = Cancellation(), asyncio.current_task()
        self.invocations[task] = (tool, token)
        started = time.monotonic()
        try:
            async with asyncio.timeout(budget):
                async with self._slot(tool.concurrency):
                    token.check()
                    result = await tool.handler(arguments, self, token)
                    token.check()
                    if not isinstance(result, dict) or len(encoded(result)) > tool.result_limit:
                        raise Fault("size_limit", "tool_result")
                    return result
        except (asyncio.CancelledError, TimeoutError):
            token.cancel()
            if tool.cancel:
                await tool.cancel(self)
            raise
        except Fault:
            raise
        except Exception:
            raise Fault("internal_error", "tool_handler") from None
        finally:
            self.invocations.pop(task, None)
            log.info("tool=%s duration_ms=%d", name, (time.monotonic() - started) * 1000)

    async def stop(self):
        async with self.stop_lock:
            self.stopping = True
            self.state = "stopping"
            current = asyncio.current_task()
            cancelled = []
            for task, (tool, token) in list(self.invocations.items()):
                if task is not current and tool.concurrency != "read":
                    token.cancel()
                    task.cancel()
                    cancelled.append(task)
            if self.turn_task and self.turn_task is not current and not self.turn_task.done():
                self.turn_task.cancel()
                cancelled.append(self.turn_task)
            await asyncio.gather(*cancelled, return_exceptions=True)
            hardware = await self.hardware.stop()
            try:
                async with Client(self.config.adapter_socket, self.config.adapter_uid) as client:
                    cloud = await client.call("turn.cancel_local", timeout=3)
            except Fault:
                cloud = {"adapter_available": False, "remote_task_cancelled": False}
            self.stopping = False
            self.state = "idle" if self.connected else "offline"
            return {"local_work_stopped": bool(cancelled) or hardware.get("local_work_stopped", False),
                    "hardware": hardware, "muse": cloud, "remote_task_cancelled": False}

    async def converse(self, text=None, seconds=None, voice=False):
        if self.stopping or self.turn_task:
            raise Fault("busy", "conversation")
        if (seconds is not None or voice) and not self.config.audio_enabled:
            raise Fault("not_ready", "audio_gate")
        self.turn_task = asyncio.current_task()
        submitted = False
        try:
            # One lock covers capture through fully drained playback: half duplex.
            async with self.locks["media"]:
                async with Client(self.config.adapter_socket, self.config.adapter_uid) as client:
                    if seconds is not None:
                        if type(seconds) not in (int, float) or not 0.1 <= seconds <= 20:
                            raise Fault("invalid_params", "recording_duration")
                        self.state = "listening"
                        wav = await self.hardware.record(seconds)
                        ref = await client.upload("voice", wav)
                        snapshot = await client.call("voice.send", {"transfer_id": ref})
                    else:
                        snapshot = await client.call("chat.send", {"text": text, "voice": voice})
                    submitted = True
                    self.state = "thinking"
                    async with asyncio.timeout(125):
                        while snapshot["state"] not in ("complete", "cancelled", "error"):
                            snapshot = await client.call("turn.watch", {"turn_id": snapshot["turn_id"],
                                "generation": snapshot["generation"], "revision": snapshot["revision"]}, timeout=15)
                    if snapshot["state"] != "complete":
                        error = snapshot.get("error", {"code": "cancelled", "stage": "turn"})
                        raise Fault(error["code"], error["stage"])
                    if voice or seconds is not None:
                        for reply in snapshot["replies"]:
                            ref = await client.call("speech.read", {"turn_id": snapshot["turn_id"],
                                "generation": snapshot["generation"], "message_id": reply["message_id"]}, timeout=65)
                            mp3 = await client.download(ref, limit=MP3_LIMIT)
                            current = await client.call("connection.status", timeout=3)
                            if current["generation"] != snapshot["generation"] or not current["connected"]:
                                raise Fault("not_connected", "stale_speech")
                            self.state = "speaking"
                            await self.hardware.play(mp3)
                    return snapshot
        finally:
            if submitted:
                with contextlib.suppress(Fault):
                    async with Client(self.config.adapter_socket, self.config.adapter_uid) as client:
                        await client.call("turn.cancel_local", timeout=3)
            self.turn_task = None
            self.state = "idle" if self.connected else "offline"

    async def handle(self, op, payload, owner, deadline):
        if op == "catalog.get":
            return self.registry.catalog()
        if op == "runtime.status":
            return await self.status()
        if op == "runtime.stop":
            return await self.stop()
        if op == "tool.invoke":
            if payload.get("catalog_hash") != self.registry.hash:
                raise Fault("worker_unavailable", "catalog_changed")
            try:
                return await self.invoke(payload.get("name"), payload.get("params", {}), payload.get("timeout_ms"))
            except TimeoutError:
                raise Fault("timeout", "tool") from None
            except asyncio.CancelledError:
                # Only report cancellation while the socket remains alive; server shutdown closes it.
                raise Fault("cancelled", "tool") from None
        if op == "conversation.run":
            if type(payload.get("voice", False)) is not bool:
                raise Fault("invalid_params", "voice")
            return await self.converse(payload.get("text"), payload.get("seconds"), payload.get("voice", False))
        raise Fault("invalid_params", "operation")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(name)s %(message)s")
    async def run():
        config = Config.load(args.config)
        if os.geteuid() == 0 or config.worker_uid != os.geteuid():
            raise Fault("invalid_params", "worker_account")
        runtime = Runtime(config)
        stop = asyncio.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            asyncio.get_running_loop().add_signal_handler(sig, stop.set)
        try:
            await runtime.start()
            await stop.wait()
        finally:
            await runtime.close()
    try:
        asyncio.run(run())
    except Fault as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()
