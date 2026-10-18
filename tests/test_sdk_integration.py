import ast
import asyncio
import importlib.util
import inspect
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from musegadget.executor import COMMAND_SPECS
from musegadget.identity import Identity
from musegadget.link_client import LinkSession, Outcome
from musegadget.service import Service

from reachy_companion.conversation import Coordinator
from reachy_companion.muse_adapter import Bridge, CompositeExecutor, validate_catalog
from reachy_companion.protocol import Fault
from reachy_companion.runtime import Runtime


def fake_vm_module():
    root = Path(__file__).resolve().parents[1]
    path = Path(os.environ.get("MUSE_SDK_TESTS", root / "artifacts/sdk-worktree/linux/tests")) / "test_link_client.py"
    spec = importlib.util.spec_from_file_location("upstream_fake_vm", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_service_defaults_and_python39_syntax():
    one = Service(Identity("00:00:00:00:00:01"), SimpleNamespace(run=lambda *a: {}))
    two = Service(one.identity, one.executor)
    assert one.command_specs == COMMAND_SPECS
    assert one.command_specs is not two.command_specs and one.session_factory is LinkSession
    import musegadget.service as service
    ast.parse(inspect.getsource(service), feature_version=(3, 9))


async def test_service_factory_catalog_and_reconnect():
    calls = []
    class Session:
        registered_at = None
        def __init__(self, **kwargs):
            calls.append(kwargs)
        async def run(self, stop):
            return Outcome.CLOSED
    catalog = {"reachy.status": {"description": "Read", "required": {}, "optional": {}}}
    service = Service(Identity("00:00:00:00:00:01"), SimpleNamespace(run=lambda *a: {}),
                      command_specs=catalog, session_factory=Session)
    for _ in range(2):
        result, _ = await service._session({"vm_id": "test", "vm_name": "test", "vm_auth_token": "fixture"}, {})
        assert result is Outcome.CLOSED
    assert len(calls) == 2 and all(c["device"].commands == catalog for c in calls)


def test_catalog_rejects_builtin_override_and_invalid_types():
    for commands in ({"system.run": COMMAND_SPECS["system.run"]}, {"demo.read": {}}):
        with pytest.raises(Fault):
            validate_catalog({"hash": "a" * 64, "commands": commands}, COMMAND_SPECS)


async def test_real_noise_fake_vm_registers_and_invokes_worker(local_config, hardware):
    fixtures = fake_vm_module()
    runtime = Runtime(local_config, hardware)
    await runtime.start()
    bridge = Bridge(local_config, Coordinator("test-chat"))
    catalog = await bridge.load_catalog()
    builtin_calls = []
    def builtin(*args):
        builtin_calls.append(args)
        return {"ok": True, "payload": {"builtin": True}}
    executor = CompositeExecutor(SimpleNamespace(account=None, run=builtin), bridge)
    to_device, to_vm = asyncio.Queue(), asyncio.Queue()
    device_ws, vm_ws = fixtures.Pipe(to_device, to_vm), fixtures.Pipe(to_vm, to_device)
    async def connect(*args):
        return device_ws
    from musegadget.link_client import DeviceDescription
    session = bridge.make_session(noise_host="example.invalid", vm_id="fixture", vm_auth_token="fixture",
        device=DeviceDescription("test-device", "test", "0", catalog), run_command=executor.run, connect=connect)
    vm, stop = fixtures.FakeVm(vm_ws), asyncio.Event()
    task = asyncio.create_task(session.run(stop))
    try:
        await vm.handshake()
        await vm.accept_control_stream()
        registration = await vm.next_message()
        assert set(registration["params"]["commands_v2"]) == set(COMMAND_SPECS) | set(runtime.registry.tools)
        await vm.send_message({"type": "res", "id": registration["id"], "ok": True})
        await asyncio.wait_for(session.registered.wait(), 1)
        for name in ("reachy.status", "device.health"):
            await vm.send_message({"method": "link.invoke", "id": name, "command": name, "params": {}, "timeout_ms": 3000})
            result = await asyncio.wait_for(vm.next_message(), 4)
            assert result["ok"] and result["id"] == name
            if name == "reachy.status":
                assert result["payload"]["ready"]
        assert builtin_calls == [("device.health", {}, 3000)]
        # Worker restart/config mismatch cannot execute custom calls under an old catalog.
        bridge.catalog["hash"] = "0" * 64
        result = await asyncio.to_thread(executor.run, "reachy.status", {}, 3000)
        assert not result["ok"] and "catalog_changed" in result["error"]
    finally:
        stop.set()
        await asyncio.wait_for(task, 2)
        await runtime.close()
    assert bridge.coordinator.session is None


async def test_worker_text_roundtrip_over_real_noise_and_adapter_ipc(local_config, hardware, monkeypatch):
    import musegadget.muse_turn as turns
    from musegadget.link_client import DeviceDescription
    from musegadget.noise import ApplicationResponse, BodyChunk, ServiceFrame
    from reachy_companion.ipc import Client
    monkeypatch.setattr(turns, "REPLY_QUIET_S", .02)
    fixtures = fake_vm_module()
    runtime = Runtime(local_config, hardware)
    await runtime.start()
    bridge = Bridge(local_config, Coordinator("test-chat"))
    await bridge.server.start()
    catalog = await bridge.load_catalog()
    to_device, to_vm = asyncio.Queue(), asyncio.Queue()
    device_ws, vm_ws = fixtures.Pipe(to_device, to_vm), fixtures.Pipe(to_vm, to_device)
    async def connect(*args):
        return device_ws
    session = bridge.make_session(noise_host="example.invalid", vm_id="fixture", vm_auth_token="fixture",
        device=DeviceDescription("test-device", "test", "0", catalog), run_command=lambda *a: {}, connect=connect)
    vm, stop = fixtures.FakeVm(vm_ws), asyncio.Event()
    task = asyncio.create_task(session.run(stop))
    try:
        await vm.handshake()
        await vm.accept_control_stream()
        registration = await vm.next_message()
        await vm.send_message({"type": "res", "id": registration["id"], "ok": True})
        await asyncio.wait_for(session.registered.wait(), 1)
        async with Client(local_config.worker_socket, os.getuid()) as operator:
            turn = asyncio.create_task(operator.call("conversation.run", {"text": "Hello"}, timeout=5))
            subscription = await asyncio.wait_for(vm.next_frame(), 2)
            assert subscription.value.path == "/chat/subscribe"
            await vm.send_frame(ServiceFrame.response(subscription.stream_id, ApplicationResponse(status=200)))
            submission = await asyncio.wait_for(vm.next_frame(), 2)
            assert submission.value.path == "/chat/stream"
            assert json.loads(submission.value.body)["session_id"] == "test-chat"
            event = {"type": "event", "event": "delta.message_done", "payload": {
                "session_id": "test-chat", "message_id": "answer", "reply_to_message_id": "question", "content": "Hello back"}}
            await vm.send_frame(ServiceFrame.body_chunk(subscription.stream_id, BodyChunk(data=json.dumps(event).encode() + b"\n")))
            await vm.send_frame(ServiceFrame.response(submission.stream_id, ApplicationResponse(
                status=200, body=b'{"message_id":"question","session_id":"test-chat"}', end_body=True)))
            result = await asyncio.wait_for(turn, 3)
            assert result["state"] == "complete" and result["replies"][0]["text"] == "Hello back"
            assert hardware.effects == []
    finally:
        stop.set()
        await asyncio.wait_for(task, 2)
        await bridge.server.close()
        await runtime.close()


async def test_worker_channel_loss_retires_active_turn(local_config):
    from reachy_companion.ipc import Client
    from test_conversation import FakeSession
    coordinator = Coordinator("chat")
    coordinator.attach(FakeSession(events=[]))
    bridge = Bridge(local_config, coordinator)
    await bridge.server.start()
    try:
        async with Client(local_config.adapter_socket, os.getuid()) as client:
            await client.call("chat.send", {"text": "Test"})
            assert coordinator.active
        for _ in range(20):
            if coordinator.active is None:
                break
            await asyncio.sleep(.01)
        assert coordinator.active is None
    finally:
        await bridge.server.close()
