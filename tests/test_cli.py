import asyncio
import json
import os
import sys

from reachy_companion.ipc import Client
from reachy_companion.protocol import Fault


async def test_packaged_worker_and_cli_without_muse(local_config):
    root = local_config.worker_socket.parent.parent
    config = root / "config.json"
    config.write_text(json.dumps({"version": 1, "worker_socket": "w/s", "adapter_socket": "m/s",
                                  "state_dir": "state", "modules": ["reachy_companion.tools.core"]}))
    process = await asyncio.create_subprocess_exec(sys.executable, "-m", "reachy_companion.runtime", "--config", str(config),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    try:
        for _ in range(100):
            if local_config.worker_socket.exists():
                break
            if process.returncode is not None:
                raise AssertionError("Worker exited before readiness")
            await asyncio.sleep(.02)
        async def cli(*args):
            command = await asyncio.create_subprocess_exec(sys.executable, "-m", "reachy_companion.cli", "--config", str(config),
                *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            output, error = await asyncio.wait_for(command.communicate(), 10)
            assert command.returncode == 0, error.decode()
            return json.loads(output)
        catalog = await cli("tools", "list")
        assert "reachy.status" in catalog["commands"]
        status = await cli("invoke", "reachy.status")
        assert status["ready"] and not status["hardware"]["audio_enabled"]
        assert not (await cli("stop"))["remote_task_cancelled"]
    finally:
        process.terminate()
        _, errors = await asyncio.wait_for(process.communicate(), 10)
        assert process.returncode == 0, errors.decode()
    assert not local_config.worker_socket.exists()
