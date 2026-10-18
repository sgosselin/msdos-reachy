import asyncio
import os
import shutil
import tempfile
from pathlib import Path

import pytest

from reachy_companion.config import Config


@pytest.fixture
def local_config():
    # Keep Unix socket names shorter than the kernel limit on macOS as well as Linux.
    root = Path(tempfile.mkdtemp(prefix="rc-")).resolve()
    try:
        yield Config(root / "w" / "s", root / "m" / "s", root / "state")
    finally:
        shutil.rmtree(root)


class FakeHardware:
    def __init__(self):
        self.effects = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.stopped = False

    async def status(self):
        return {"daemon": "test", "media_available": False}

    async def stop(self):
        self.stopped = True
        return {"local_work_stopped": False}

    async def camera(self, size, quality):
        self.started.set()
        await self.release.wait()
        self.effects.append("camera")
        return {"size": size}

    async def emote(self, preset, intensity):
        self.started.set()
        await self.release.wait()
        self.effects.append("motion")
        return {"preset": preset}


@pytest.fixture
def hardware():
    return FakeHardware()
