import io
import asyncio
import sys
import wave

import numpy as np
import pytest

from reachy_companion.hardware.formats import jpeg_result, pcm_wav, validate_wav
from reachy_companion.hardware.reachy import Hardware
from reachy_companion.protocol import CLOUD_LIMIT, Fault, encoded


def test_stereo_float_to_mono_pcm_wav():
    samples = np.array([[1., -1.], [2., 2.], [-2., -2.]], dtype=np.float32)
    data = pcm_wav(samples)
    validate_wav(data)
    with wave.open(io.BytesIO(data)) as wav:
        assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) == (1, 2, 16000)
        assert np.frombuffer(wav.readframes(3), dtype="<i2").tolist() == [0, 32767, -32767]


@pytest.mark.parametrize("samples", [np.zeros((320001, 2)), np.array([[float("nan"), 0]]), np.zeros(5)])
def test_bad_or_long_recording_rejected(samples):
    with pytest.raises(Fault):
        pcm_wav(samples)


def test_truncated_wav_rejected():
    with pytest.raises(Fault):
        validate_wav(pcm_wav(np.zeros((100, 2)))[:-10])


def test_camera_enforces_whole_serialized_result_limit():
    frame = np.random.default_rng(1).integers(0, 256, (720, 1280, 3), dtype=np.uint8)
    result = jpeg_result(frame, 1280, 90)
    assert len(encoded({"ok": True, "payload": result})) < CLOUD_LIMIT
    assert result["format"] == "jpeg-base64" and result["captured_at"]


async def test_default_hardware_gates_precede_device_access(local_config):
    hardware = Hardware(local_config)
    for action in (hardware.camera(640, 75), hardware.record(1), hardware.play(b"x"), hardware.emote("nod", .5)):
        with pytest.raises(Fault, match="gate"):
            await action
    assert not hardware.children


async def test_cancellation_reaps_blocked_hardware_child(local_config, monkeypatch):
    hardware = Hardware(local_config)
    real_create = asyncio.create_subprocess_exec
    async def blocked_child(*args, **kwargs):
        return await real_create(sys.executable, "-c", "import time; time.sleep(30)", **kwargs)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", blocked_child)
    task = asyncio.create_task(hardware._run("camera", {}, 10))
    while not hardware.children:
        await asyncio.sleep(.01)
    child = next(iter(hardware.children))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert child.returncode is not None and not hardware.children
