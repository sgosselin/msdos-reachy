"""Muse service launcher and thread-to-worker bridge. Never imports plugins."""

import argparse
import asyncio
import concurrent.futures
import copy
import logging
import os
import re
import signal
import stat

from .config import Config, chat_id
from .conversation import Coordinator
from .ipc import Client, Server
from .protocol import CLOUD_LIMIT, Fault, encoded

log = logging.getLogger(__name__)


def validate_catalog(catalog, builtins):
    try:
        commands, digest = catalog["commands"], catalog["hash"]
        if (not isinstance(commands, dict) or not commands or len(commands) > 64
                or not re.fullmatch(r"[0-9a-f]{64}", digest) or commands.keys() & builtins.keys()):
            raise ValueError
        for name, spec in commands.items():
            if (not re.fullmatch(r"[a-z][a-z0-9_.]{1,63}", name)
                    or set(spec) != {"description", "required", "optional", "timeout_ms"}
                    or not isinstance(spec["description"], str) or not spec["description"]
                    or type(spec["timeout_ms"]) is not int or not 1 <= spec["timeout_ms"] <= 120000
                    or not isinstance(spec["required"], dict) or not isinstance(spec["optional"], dict)
                    or spec["required"].keys() & spec["optional"].keys()):
                raise ValueError
            for key, param in {**spec["required"], **spec["optional"]}.items():
                if (not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", key)
                        or set(param) != {"type", "description"}
                        or param["type"] not in ("string", "integer", "number", "boolean")
                        or not isinstance(param["description"], str)):
                    raise ValueError
        if len(encoded(catalog)) > 48 * 1024:
            raise ValueError
        return copy.deepcopy(catalog)
    except (KeyError, TypeError, ValueError):
        raise Fault("invalid_params", "worker_catalog") from None


class CompositeExecutor:
    def __init__(self, builtin, bridge):
        self.builtin, self.bridge, self.account = builtin, bridge, builtin.account

    def run(self, command, params, timeout_ms=None):
        from musegadget.executor import COMMAND_SPECS
        if command in COMMAND_SPECS:
            return self.builtin.run(command, params, timeout_ms)
        if command not in self.bridge.catalog["commands"]:
            return {"ok": False, "error": "unknown_tool"}
        if timeout_ms is not None and (type(timeout_ms) is not int or timeout_ms <= 0):
            return {"ok": False, "error": "invalid_params: timeout"}
        timeout = min(timeout_ms or 120000, self.bridge.catalog["commands"][command]["timeout_ms"]) / 1000
        future = asyncio.run_coroutine_threadsafe(self.bridge.invoke(command, params, timeout), self.bridge.loop)
        try:
            payload = future.result(timeout=timeout + 1)
            result = {"ok": True, "payload": payload}
            if len(encoded(result)) > CLOUD_LIMIT:
                raise Fault("size_limit", "cloud_result")
            return result
        except (concurrent.futures.TimeoutError, concurrent.futures.CancelledError):
            future.cancel()
            return {"ok": False, "error": "outcome_unknown: worker_timeout"}
        except Fault as exc:
            return {"ok": False, "error": str(exc)}
        except Exception:
            return {"ok": False, "error": "worker_unavailable"}


class Bridge:
    def __init__(self, config, coordinator):
        self.config, self.coordinator = config, coordinator
        self.catalog = {"commands": {}}
        self.loop = asyncio.get_running_loop()
        self.server = Server(config.adapter_socket, self.handle,
                             allowed_uids={os.getuid(), config.worker_uid}, gid=config.socket_gid,
                             on_disconnect=self.disconnected)
        self.pending = set()
        self.turn_owner = None

    async def disconnected(self, owner):
        if self.turn_owner is owner:
            self.turn_owner = None
            await self.coordinator.cancel("outcome_unknown", "worker_disconnect")

    async def load_catalog(self):
        from musegadget.executor import COMMAND_SPECS
        async with asyncio.timeout(30):
            while True:
                try:
                    async with Client(self.config.worker_socket, self.config.worker_uid) as client:
                        self.catalog = validate_catalog(await client.call("catalog.get", timeout=3), COMMAND_SPECS)
                    return {**copy.deepcopy(COMMAND_SPECS), **copy.deepcopy(self.catalog["commands"])}
                except Fault as exc:
                    log.info("catalog_not_ready code=%s stage=%s", exc.code, exc.stage)
                    await asyncio.sleep(1)

    async def invoke(self, name, params, timeout):
        task = asyncio.current_task()
        self.pending.add(task)
        try:
            async with Client(self.config.worker_socket, self.config.worker_uid) as client:
                return await client.call("tool.invoke", {"name": name, "params": params,
                    "catalog_hash": self.catalog["hash"], "timeout_ms": max(1, int(timeout * 1000))}, timeout=timeout)
        finally:
            self.pending.discard(task)

    def make_session(self, **kwargs):
        from musegadget.link_client import LinkSession
        bridge = self
        class CompanionSession(LinkSession):
            async def run(self, stop):
                bridge.coordinator.attach(self)
                log.info("connection generation=%s", bridge.coordinator.generation)
                try:
                    return await super().run(stop)
                finally:
                    await bridge.coordinator.detach(self)
                    for task in list(bridge.pending):
                        task.cancel()
                    await asyncio.gather(*bridge.pending, return_exceptions=True)
                    bridge.server.transfers.items.clear()
        return CompanionSession(**kwargs)

    async def handle(self, op, payload, owner, deadline):
        coordinator = self.coordinator
        if op == "connection.status":
            return {**coordinator.connection(), "catalog_hash": self.catalog.get("hash")}
        if op == "chat.send":
            if type(payload.get("voice", False)) is not bool:
                raise Fault("invalid_params", "voice")
            result = coordinator.begin(text=payload.get("text"), voice=payload.get("voice", False))
            self.turn_owner = owner
            return result
        if op == "voice.send":
            from .hardware.formats import validate_wav  # stdlib only until conversion is called
            data = self.server.transfers.take(owner, payload.get("transfer_id"), "voice")
            validate_wav(data)
            result = coordinator.begin(wav=data, voice=True)
            self.turn_owner = owner
            return result
        if op == "turn.watch":
            return await coordinator.watch(payload.get("turn_id"), payload.get("generation"), payload.get("revision"))
        if op == "turn.cancel_local":
            result = await coordinator.cancel()
            self.server.transfers.items.clear()
            return result
        if op == "speech.read":
            data = await coordinator.speech(payload.get("turn_id"), payload.get("generation"), payload.get("message_id"))
            return self.server.transfers.put(owner, "speech", data)
        if op == "chat.new":
            if coordinator.active or coordinator.speech_task:
                raise Fault("busy", "turn")
            await coordinator.cancel()
            coordinator.history.clear()
            coordinator.chat_id = chat_id(self.config.state_dir, new=True)
            return {"chat_id": coordinator.chat_id}
        raise Fault("invalid_params", "operation")


async def legacy_available(path):
    """Refuse to take the legacy socket from a running stock service."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.geteuid():
        raise Fault("invalid_params", "legacy_socket")
    try:
        _, writer = await asyncio.wait_for(asyncio.open_unix_connection(str(path)), 1)
    except (ConnectionRefusedError, FileNotFoundError):
        return
    writer.close()
    await writer.wait_closed()
    raise Fault("busy", "existing_muse_service")


async def serve(config):
    from musegadget import config as sdk_config, identity
    from musegadget.executor import Account, Executor
    from musegadget.service import Service
    if "session_factory" not in Service.__dataclass_fields__:
        raise Fault("not_ready", "sdk_patch_missing")
    if os.geteuid() == 0 and not config.run_as:
        raise Fault("invalid_params", "command_account")
    account = Account.lookup(config.run_as) if config.run_as else Account.current()
    if os.geteuid() == 0 and account.uid == 0:
        raise Fault("invalid_params", "command_account")
    if config.adapter_uid != os.geteuid() or os.geteuid() == 0 and config.worker_uid == 0:
        raise Fault("invalid_params", "adapter_account")
    bridge = Bridge(config, Coordinator(None))
    stop = asyncio.Event()
    service = None
    legacy = None
    def stop_all():
        stop.set()
        if service:
            service.stop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        asyncio.get_running_loop().add_signal_handler(sig, stop_all)
    await bridge.server.start()
    try:
        bridge.coordinator.chat_id = chat_id(config.state_dir)
        await legacy_available(sdk_config.socket_path())
        while not stop.is_set():
            try:
                catalog_task = asyncio.create_task(bridge.load_catalog())
                stopped = asyncio.create_task(stop.wait())
                done, _ = await asyncio.wait((catalog_task, stopped), return_when=asyncio.FIRST_COMPLETED)
                if stopped in done:
                    catalog_task.cancel()
                    await asyncio.gather(catalog_task, return_exceptions=True)
                    return
                stopped.cancel()
                await asyncio.gather(stopped, return_exceptions=True)
                catalog = await catalog_task
                break
            except TimeoutError:
                log.warning("catalog_not_ready retrying")
        else:
            return
        service = Service(identity=identity.load_or_create(), executor=CompositeExecutor(Executor(account), bridge),
                          sdk_token=sdk_config.sdk_token(), command_specs=catalog, session_factory=bridge.make_session)
        legacy = await service.serve_local(sdk_config.socket_path())
        await service.run()
    finally:
        await bridge.coordinator.cancel()
        await bridge.server.close()
        if legacy:
            legacy.close()
            await legacy.wait_closed()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(name)s %(message)s")
    try:
        asyncio.run(serve(Config.load(args.config)))
    except Fault as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()
