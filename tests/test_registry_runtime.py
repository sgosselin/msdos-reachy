import asyncio
import sys
from types import SimpleNamespace

import pytest

from reachy_companion.protocol import Fault
from reachy_companion.registry import Parameter, Registry, Tool
from reachy_companion.runtime import Runtime


async def handler(params, context, cancellation):
    return params


def test_catalog_freezes_and_hash_includes_local_constraints():
    first, second = Registry(), Registry()
    first.add(Tool("demo.read", "Read", handler, optional={"n": Parameter("integer", "N", maximum=2)}))
    second.add(Tool("demo.read", "Read", handler, optional={"n": Parameter("integer", "N", maximum=3)}))
    first.freeze()
    second.freeze()
    assert first.hash != second.hash
    assert first.catalog()["commands"] == second.catalog()["commands"]
    first.catalog()["commands"].clear()
    assert first.catalog()["commands"]
    with pytest.raises(Fault):
        first.add(Tool("demo.more", "More", handler))


@pytest.mark.parametrize("name", ["device.health", "system.run", "file.read", "file.write", "Bad Name"])
def test_reserved_and_invalid_names(name):
    with pytest.raises(Fault):
        Registry().add(Tool(name, "Read", handler))


def test_duplicate():
    registry = Registry()
    registry.add(Tool("demo.read", "Read", handler))
    with pytest.raises(Fault):
        registry.add(Tool("demo.read", "Read again", handler))


@pytest.mark.parametrize("value", [True, 1.5, "1", -1, 4, None])
def test_integer_validation(value):
    with pytest.raises(Fault):
        Parameter("integer", "N", minimum=0, maximum=3).validate(value)


@pytest.mark.parametrize("params", [{}, {"n": 1, "extra": 0}])
def test_required_and_unknown_arguments(params):
    with pytest.raises(Fault):
        Tool("demo.read", "Read", handler, required={"n": Parameter("integer", "N")}).validate(params)


async def test_worker_read_only_status(local_config, hardware):
    runtime = Runtime(local_config, hardware)
    await runtime.start()
    try:
        result = await runtime.invoke("reachy.status", {})
        assert result["ready"] and result["hardware"]["daemon"] == "test"
        assert hardware.effects == []
        assert set(runtime.registry.tools) == {"reachy.status", "reachy.stop", "reachy.emote", "reachy.camera.capture"}
    finally:
        await runtime.close()


async def test_stop_preempts_active_and_queued_motion(local_config, hardware):
    runtime = Runtime(local_config, hardware)
    await runtime.start()
    first = asyncio.create_task(runtime.invoke("reachy.emote", {"preset": "nod"}))
    await hardware.started.wait()
    second = asyncio.create_task(runtime.invoke("reachy.emote", {"preset": "curious"}))
    await asyncio.sleep(.01)
    result = await runtime.invoke("reachy.stop", {})
    hardware.release.set()
    outcomes = await asyncio.gather(first, second, return_exceptions=True)
    assert all(isinstance(x, asyncio.CancelledError) for x in outcomes)
    assert result["local_work_stopped"] and hardware.effects == []
    assert not result["remote_task_cancelled"]
    await runtime.close()


async def test_timeout_queued_handler_never_runs(local_config, hardware):
    runtime = Runtime(local_config, hardware)
    await runtime.start()
    await runtime.locks["media"].acquire()
    try:
        with pytest.raises(TimeoutError):
            await runtime.invoke("reachy.camera.capture", {}, timeout_ms=20)
    finally:
        runtime.locks["media"].release()
    hardware.release.set()
    await asyncio.sleep(.02)
    assert not hardware.started.is_set() and hardware.effects == []
    await runtime.close()


async def test_failed_module_start_unwinds_in_reverse(local_config, hardware, monkeypatch):
    events = []
    def module(name, fail=False):
        async def start(context):
            events.append("start-" + name)
            if fail:
                raise RuntimeError("private diagnostic that must not be returned")
        async def stop():
            events.append("stop-" + name)
        return SimpleNamespace(register=lambda registry: None, start=start, stop=stop)
    monkeypatch.setitem(sys.modules, "first_plugin", module("first"))
    monkeypatch.setitem(sys.modules, "second_plugin", module("second", True))
    local_config.modules = ("first_plugin", "second_plugin")
    runtime = Runtime(local_config, hardware)
    with pytest.raises(RuntimeError):
        await runtime.start()
    assert events == ["start-first", "start-second", "stop-second", "stop-first"]
    assert not runtime.ready
