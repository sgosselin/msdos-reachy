import asyncio
import base64
import os
import time
import uuid

import pytest

from reachy_companion.ipc import Client, Server
from reachy_companion.protocol import CHUNK, CLOUD_LIMIT, LINE_LIMIT, Fault, Transfers, line, request
from reachy_companion.runtime import Runtime


def begin(store, owner, total=2, purpose="voice"):
    from reachy_companion.protocol import TRANSFER_LIMITS
    return store.begin(owner, {"transfer_id": str(uuid.uuid4()), "purpose": purpose,
                              "type": TRANSFER_LIMITS[purpose][0], "total": total})["transfer_id"]


def test_transfer_ownership_sequence_size_and_expiry():
    store, owner = Transfers(), object()
    key = begin(store, owner)
    for actor, offset, data in [(object(), 0, b"x"), (owner, 1, b"x"), (owner, 0, b"xxx")]:
        with pytest.raises(Fault):
            store.dispatch(actor, "transfer.chunk", {"transfer_id": key, "offset": offset,
                           "data": base64.b64encode(data).decode()})
    with pytest.raises(Fault):
        store.dispatch(owner, "transfer.end", {"transfer_id": key})
    store.dispatch(owner, "transfer.chunk", {"transfer_id": key, "offset": 0, "data": "eHg="})
    store.dispatch(owner, "transfer.end", {"transfer_id": key})
    with pytest.raises(Fault):
        store.take(owner, key, "speech")
    assert store.take(owner, key, "voice") == b"xx"
    key = begin(store, owner)
    store.items[key].touched -= 16
    with pytest.raises(Fault):
        store.get(owner, key)


def test_process_quotas_and_disconnect_cleanup():
    store, a, b = Transfers(), object(), object()
    begin(store, a)
    with pytest.raises(Fault, match="busy"):
        begin(store, b)  # only one voice upload across connections
    begin(store, b, purpose="speech")
    begin(store, a, purpose="tool-result")
    with pytest.raises(Fault, match="busy"):
        begin(store, b, purpose="tool-result")
    store.discard(a)
    assert len(store.items) == 1


def test_line_and_deadline_limits():
    with pytest.raises(Fault, match="size_limit"):
        line({"data": "x" * LINE_LIMIT})
    with pytest.raises(Fault, match="timeout"):
        request({"v": 1, "id": str(uuid.uuid4()), "op": "x", "payload": {}, "deadline": time.time() - 1})


async def test_large_result_chunks_roundtrip_and_upload(local_config):
    content = {"jpeg": "x" * (180 * 1024)}
    async def handle(op, payload, owner, deadline):
        if op == "large":
            return content
        return {"bytes": len(server.transfers.take(owner, payload["transfer_id"], "voice"))}
    server = Server(local_config.worker_socket, handle, allowed_uids={os.getuid()})
    await server.start()
    try:
        async with Client(local_config.worker_socket, os.getuid()) as client:
            assert await client.call("large") == content
            key = await client.upload("voice", b"x" * (CHUNK * 3 + 1))
            assert await client.call("consume", {"transfer_id": key}) == {"bytes": CHUNK * 3 + 1}
            assert not server.transfers.items
    finally:
        await server.close()


async def test_disconnect_cancels_before_effect(local_config, hardware):
    runtime = Runtime(local_config, hardware)
    await runtime.start()
    async with Client(local_config.worker_socket, os.getuid()) as client:
        task = asyncio.create_task(client.call("tool.invoke", {"catalog_hash": runtime.registry.hash,
                                  "name": "reachy.camera.capture", "params": {}}))
        await hardware.started.wait()
        await client.close()
        with pytest.raises(Fault, match="outcome_unknown"):
            await task
    await asyncio.sleep(.05)
    hardware.release.set()
    assert not hardware.effects
    await runtime.close()


async def test_peer_identity_and_duplicate_server_refused(local_config):
    async def handler(*args):
        return {}
    server = Server(local_config.worker_socket, handler, allowed_uids={os.getuid()})
    await server.start()
    try:
        with pytest.raises(Fault, match="peer_identity"):
            async with Client(local_config.worker_socket, os.getuid() + 1):
                pass
        second = Server(local_config.worker_socket, handler, allowed_uids={os.getuid()})
        with pytest.raises(BlockingIOError):
            await second.start()
        async with Client(local_config.worker_socket, os.getuid()) as client:
            assert await client.call("ok") == {}
    finally:
        await server.close()


async def test_malformed_oversized_and_duplicate_requests(local_config):
    calls = []
    async def handler(*args):
        calls.append(1)
        return {}
    server = Server(local_config.worker_socket, handler, allowed_uids={os.getuid()})
    await server.start()
    try:
        for raw in (b"[]\n", b"x" * (LINE_LIMIT + 1) + b"\n"):
            reader, writer = await asyncio.open_unix_connection(str(local_config.worker_socket))
            writer.write(raw)
            await writer.drain()
            assert await asyncio.wait_for(reader.read(), 1) == b""
            writer.close()
            await writer.wait_closed()
        reader, writer = await asyncio.open_unix_connection(str(local_config.worker_socket))
        data = line({"v": 1, "id": str(uuid.uuid4()), "op": "go", "payload": {}, "deadline": time.time() + 5})
        writer.write(data)
        await writer.drain()
        await reader.readline()
        writer.write(data)
        await writer.drain()
        assert b"duplicate_request" in await reader.readline()
        assert calls == [1]
        writer.close()
        await writer.wait_closed()
    finally:
        await server.close()
