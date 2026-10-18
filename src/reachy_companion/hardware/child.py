"""One bounded hardware operation per process. Input/output stay in memory."""

import base64
import json
import math
import signal
import sys
import time
from datetime import datetime, timezone

from ..protocol import MP3_LIMIT, Fault, encoded
from .formats import jpeg_result, pcm_wav
from .reachy import daemon_get, preflight

cancelled = False


def interrupt(*_):
    global cancelled
    cancelled = True


def check():
    if cancelled:
        raise Fault("cancelled", "hardware")


def camera(payload):
    from reachy_mini.media.camera_constants import get_camera_specs_by_name
    from reachy_mini.media.camera_gstreamer import GStreamerCamera
    daemon = preflight()
    device = GStreamerCamera(camera_specs=get_camera_specs_by_name(daemon["camera_specs_name"]), log_level="ERROR")
    try:
        device.open()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            check()
            frame = device.read()
            if frame is not None:
                captured = datetime.now(timezone.utc).isoformat()
                return jpeg_result(frame, payload["size"], payload["quality"], captured)
            time.sleep(.02)
        raise Fault("timeout", "camera_frame")
    finally:
        device.close()


def audio_preflight():
    from reachy_mini.media.audio_utils import has_reachymini_asoundrc
    preflight()
    if not has_reachymini_asoundrc():
        raise Fault("not_ready", "alsa_routes")


def record(payload):
    import numpy as np
    from reachy_mini.media.audio_gstreamer import GStreamerAudio
    audio_preflight()
    seconds = payload["seconds"]
    if type(seconds) not in (int, float) or not .1 <= seconds <= 20:
        raise Fault("invalid_params", "recording_duration")
    device = GStreamerAudio(log_level="ERROR")
    try:
        rate, channels = device.get_input_audio_samplerate(), device.get_input_channels()
        if (rate, channels) != (16000, 2):
            raise Fault("not_ready", "audio_format")
        device.start_recording()
        samples, count = [], 0
        target, discard = round(seconds * rate), rate // 5
        deadline = time.monotonic() + seconds + 4
        while count < target and time.monotonic() < deadline:
            check()
            data = device.get_audio_sample()
            if data is None:
                time.sleep(.005)
                continue
            if discard:
                skip = min(discard, len(data))
                data, discard = data[skip:], discard - skip
            if len(data):
                data = data[:target - count].copy()
                samples.append(data)
                count += len(data)
        if count != target:
            raise Fault("timeout", "microphone")
        return {"wav": base64.b64encode(pcm_wav(np.concatenate(samples))).decode()}
    finally:
        device.stop_recording()
        device.cleanup()


def _gst():
    import gi
    gi.require_version("Gst", "1.0")
    from gi.repository import Gst
    Gst.init(None)
    return Gst


def decode_mp3(data, rate, channels):
    """Decode in memory; bound decoded duration before opening the speaker."""
    Gst = _gst()
    pipeline = Gst.parse_launch(
        f"appsrc name=source ! decodebin ! audioconvert ! audioresample ! "
        f"audio/x-raw,format=F32LE,rate={rate},channels={channels},layout=interleaved ! "
        "appsink name=output sync=false max-buffers=8 drop=false")
    source, sink, bus = pipeline.get_by_name("source"), pipeline.get_by_name("output"), pipeline.get_bus()
    output = bytearray()
    try:
        pipeline.set_state(Gst.State.PLAYING)
        source.emit("push-buffer", Gst.Buffer.new_wrapped(data))
        source.emit("end-of-stream")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            check()
            sample = sink.emit("try-pull-sample", 100 * Gst.MSECOND)
            if sample:
                buffer = sample.get_buffer()
                if len(output) + buffer.get_size() > rate * channels * 4 * 60:
                    raise Fault("size_limit", "decoded_audio")
                output.extend(buffer.extract_dup(0, buffer.get_size()))
            elif sink.get_property("eos"):
                break
            if bus.pop_filtered(Gst.MessageType.ERROR):
                raise Fault("not_ready", "mp3_decoder")
        else:
            raise Fault("timeout", "mp3_decode")
        if not output:
            raise Fault("not_ready", "empty_speech")
        return bytes(output)
    finally:
        pipeline.set_state(Gst.State.NULL)


def play(payload):
    import numpy as np
    from reachy_mini.media.audio_base import AudioBase
    audio_preflight()
    volume = daemon_get("/api/volume/current").get("volume", 0)
    if not 0 < volume <= 40:
        raise Fault("not_ready", "speaker_volume")
    data = base64.b64decode(payload["mp3"], validate=True)
    if not 0 < len(data) <= MP3_LIMIT:
        raise Fault("size_limit", "speech")
    rate, channels = AudioBase.SAMPLE_RATE, AudioBase.CHANNELS
    pcm = decode_mp3(data, rate, channels)
    samples = np.frombuffer(pcm, dtype="<f4")
    if not np.isfinite(samples).all():
        raise Fault("invalid_params", "decoded_audio")
    # Attenuate unusually hot output; never amplify quiet input or change system volume.
    gain = min(1., .5 / max(float(np.abs(samples).max()), 1e-9))
    pcm = (samples * gain).astype("<f4").tobytes()
    Gst = _gst()
    pipeline = Gst.parse_launch(
        f'appsrc name=source format=time caps="audio/x-raw,format=F32LE,rate={rate},channels={channels},layout=interleaved" '
        '! audioconvert ! audioresample ! alsasink device=reachymini_audio_sink sync=true')
    try:
        source, bus = pipeline.get_by_name("source"), pipeline.get_bus()
        pipeline.set_state(Gst.State.PLAYING)
        buffer = Gst.Buffer.new_wrapped(pcm)
        buffer.pts = 0
        buffer.duration = len(pcm) * Gst.SECOND // (rate * channels * 4)
        if source.emit("push-buffer", buffer) != Gst.FlowReturn.OK:
            raise Fault("not_ready", "speaker_queue")
        source.emit("end-of-stream")
        deadline = time.monotonic() + 62
        while time.monotonic() < deadline:
            check()
            message = bus.timed_pop_filtered(100 * Gst.MSECOND, Gst.MessageType.EOS | Gst.MessageType.ERROR)
            if message:
                if message.type == Gst.MessageType.ERROR:
                    raise Fault("not_ready", "speaker")
                return {"playback": "drained", "seconds": len(pcm) / (rate * channels * 4)}
        raise Fault("timeout", "playback_drain")
    finally:
        pipeline.set_state(Gst.State.NULL)


def emote(payload):
    """Stream bounded offsets from the current pose; never enqueue a daemon goto task."""
    import numpy as np
    from reachy_mini.io.ws_client import WSClient
    from reachy_mini.io.protocol import SetTargetCmd
    preflight(media=False)
    if daemon_get("/api/motors/status").get("mode") != "enabled":
        raise Fault("not_ready", "motors_disabled")
    preset, intensity = payload["preset"], payload["intensity"]
    if preset not in ("nod", "curious") or type(intensity) not in (int, float) or not 0 <= intensity <= 1:
        raise Fault("invalid_params", "preset")
    check()
    robot = WSClient(host="localhost", port=8000)
    try:
        robot.wait_for_connection(timeout=2)
        start = robot.get_current_head_pose().copy()
        if not np.isfinite(start).all():
            raise Fault("not_ready", "pose")
        try:
            for index in range(61):
                check()
                angle = math.radians(4 if preset == "nod" else 5) * intensity * math.sin(math.pi * index / 60) ** 2
                c, s = math.cos(angle), math.sin(angle)
                rotation = np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]]) if preset == "nod" else np.array([[1, 0, 0], [0, c, -s], [0, s, c]])
                target = start.copy()
                target[:3, :3] = start[:3, :3] @ rotation
                robot.send_command(SetTargetCmd(head=target.flatten().tolist()))
                time.sleep(.02)
            return {"preset": preset, "completed": True, "physical_motion_verified": False}
        finally:
            if cancelled:
                # Hold the observed pose. This is local stop, not a torque-off/emergency stop.
                robot.send_command(SetTargetCmd(head=robot.get_current_head_pose().flatten().tolist()))
                time.sleep(.05)
    finally:
        robot.disconnect()


def main():
    signal.signal(signal.SIGTERM, interrupt)
    signal.signal(signal.SIGINT, interrupt)
    try:
        payload = json.loads(sys.stdin.buffer.read(12 * 1024 * 1024))
        result = {"camera": camera, "record": record, "play": play, "emote": emote}[sys.argv[1]](payload)
    except Exception as exc:
        result = {"error": (exc if isinstance(exc, Fault) else Fault("not_ready", "hardware")).as_dict()}
    sys.stdout.buffer.write(encoded(result))
    sys.stdout.flush()


if __name__ == "__main__":
    main()
