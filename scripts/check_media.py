#!/usr/bin/env python3
"""Local media checks for the inspected Reachy Mini SDK 1.9.0.

Run on Reachy with the app environment's interpreter. Camera mode reports metadata;
audio mode plays a cue, records five seconds, and plays the recording back.
Captured media stays in process memory unless --save-image is supplied for a
camera snapshot. Neither mode commands robot motion.
The audio mode needs an observer beside Reachy to speak and assess playback.
"""

import argparse
import json
import logging
import signal
import time
from importlib.metadata import version
from pathlib import Path
from urllib.request import ProxyHandler, build_opener


def status(path):
    opener = build_opener(ProxyHandler({}))
    with opener.open("http://127.0.0.1:8000" + path, timeout=3) as response:
        raw = response.read(65537)
    if len(raw) > 65536:
        raise RuntimeError("Status response exceeded the expected size")
    return json.loads(raw)


def preflight():
    if version("reachy-mini") != "1.9.0":
        raise RuntimeError("Review this check against the installed SDK before running")
    if status("/api/apps/current-app-status") is not None:
        raise RuntimeError("A managed app is active; coordinate media access first")
    daemon = status("/api/daemon/status")
    if daemon.get("state") != "running" or daemon.get("error"):
        raise RuntimeError("Reachy daemon is not in a healthy running state")
    if daemon.get("no_media") or daemon.get("media_released"):
        raise RuntimeError("Daemon media is disabled or released")
    return daemon


def next_frame(camera, jpeg=False):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        frame = camera.read_jpeg() if jpeg else camera.read()
        if frame is not None:
            return frame
        time.sleep(0.02)
    raise RuntimeError("No camera frame arrived within five seconds")


def check_camera(daemon, save_image=None):
    import numpy as np
    from reachy_mini.media.camera_constants import get_camera_specs_by_name
    from reachy_mini.media.camera_gstreamer import GStreamerCamera

    specs = get_camera_specs_by_name(daemon["camera_specs_name"])
    camera = GStreamerCamera(camera_specs=specs, log_level="WARNING")
    try:
        camera.open()
        frame = next_frame(camera)
        if frame.ndim != 3 or frame.shape[2] != 3 or frame.dtype != np.uint8:
            raise RuntimeError("Unexpected BGR camera frame format")
        if frame.size == 0:
            raise RuntimeError("Camera returned an empty frame")
        jpeg = next_frame(camera, jpeg=True)
        if not (jpeg.startswith(b"\xff\xd8") and jpeg.endswith(b"\xff\xd9")):
            raise RuntimeError("Camera JPEG is missing its start/end markers")
        if save_image is not None:
            save_image.write_bytes(jpeg)
        return {
            "check": "camera",
            "result": "frame_and_jpeg_received",
            "width": int(frame.shape[1]),
            "height": int(frame.shape[0]),
            "format": "uint8 BGR",
            "pixel_mean": round(float(frame.mean()), 2),
            "pixel_stddev": round(float(frame.std()), 2),
            "jpeg_bytes": len(jpeg),
            "scene_content_reviewed": False,
            "saved_image": str(save_image) if save_image is not None else None,
            "media_uploaded_by_check": False,
        }
    finally:
        camera.close()


def collect_audio(audio, sample_count, channels, seconds_limit):
    import numpy as np

    deadline = time.monotonic() + seconds_limit
    chunks = []
    count = 0
    while count < sample_count and time.monotonic() < deadline:
        chunk = audio.get_audio_sample()
        if chunk is None:
            continue
        if chunk.ndim != 2 or chunk.shape[1] != channels:
            raise RuntimeError("Unexpected microphone channel layout")
        chunk = chunk[: sample_count - count]
        if not np.isfinite(chunk).all():
            raise RuntimeError("Microphone returned non-finite samples")
        chunks.append(chunk.copy())
        count += len(chunk)
    if count != sample_count:
        raise RuntimeError(f"Microphone returned {count}/{sample_count} sample frames")
    return np.concatenate(chunks)


def play_audio(audio, samples, rate):
    # Feed in 20 ms chunks; the final wait is a bounded smoke-test drain margin,
    # not a playback-completion API for the eventual conversation application.
    audio.start_playing()
    try:
        time.sleep(0.2)
        started = time.monotonic()
        step = max(1, rate // 50)
        for offset in range(0, len(samples), step):
            audio.push_audio_sample(samples[offset : offset + step])
            due = started + min(offset + step, len(samples)) / rate
            time.sleep(max(0, due - time.monotonic()))
        time.sleep(0.5)
    finally:
        audio.stop_playing()


def check_audio():
    import numpy as np
    from reachy_mini.media.audio_gstreamer import GStreamerAudio
    from reachy_mini.media.audio_utils import has_reachymini_asoundrc

    if not has_reachymini_asoundrc():
        raise RuntimeError("Reachy-specific ALSA routes are missing")
    speaker = status("/api/volume/current")
    microphone = status("/api/volume/microphone/current")
    if not 0 < speaker["volume"] <= 40:
        raise RuntimeError("Set Reachy's speaker to a low audible level (1–40%) first")
    audio = GStreamerAudio(log_level="WARNING")
    try:
        rate = audio.get_input_audio_samplerate()
        channels = audio.get_input_channels()
        if (rate, channels) != (
            audio.get_output_audio_samplerate(), audio.get_output_channels()
        ):
            raise RuntimeError("Loopback requires matching input/output formats")
        if (rate, channels) != (16000, 2):
            raise RuntimeError("Unexpected audio format; review before proceeding")

        print("Soft cue, then five seconds of recording. Speak after the cue.", flush=True)
        t = np.arange(int(rate * 0.2), dtype=np.float32) / rate
        cue = 0.08 * np.sin(2 * np.pi * 660 * t) * np.hanning(len(t))
        play_audio(audio, np.repeat(cue[:, None], channels, axis=1).astype(np.float32), rate)
        audio.start_recording()
        try:
            # Discard startup/queued cue samples before collecting the utterance.
            collect_audio(audio, rate // 5, channels, 3)
            print("RECORDING", flush=True)
            recording = collect_audio(audio, rate * 5, channels, 8)
        finally:
            audio.stop_recording()

        peak = float(np.max(np.abs(recording)))
        rms = float(np.sqrt(np.mean(recording.astype(np.float64) ** 2)))
        # Attenuate peaks when necessary; do not amplify a silent/noisy capture.
        gain = min(1.0, 0.25 / max(peak, 1e-9))
        print("PLAYBACK", flush=True)
        play_audio(audio, (recording * gain).astype(np.float32), rate)
        return {
            "check": "audio",
            "result": "captured_and_queued_for_playback_observer_confirmation_required",
            "sample_rate": rate,
            "channels": channels,
            "sample_frames": len(recording),
            "seconds": len(recording) / rate,
            "peak": round(peak, 6),
            "rms": round(rms, 6),
            "playback_gain": round(gain, 4),
            "speaker_volume": speaker["volume"],
            "microphone_volume": microphone["volume"],
            "media_saved_or_uploaded": False,
        }
    finally:
        audio.stop_recording()
        audio.stop_playing()
        audio.cleanup()


def interrupt(signum, frame):
    raise KeyboardInterrupt(f"Media check interrupted by signal {signum}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("check", choices=("camera", "audio"))
    parser.add_argument("--save-image", type=Path, help="Save a camera JPEG to this path")
    args = parser.parse_args()
    if args.save_image is not None and args.check != "camera":
        parser.error("--save-image applies only to the camera check")
    logging.basicConfig(level=logging.WARNING)
    signal.signal(signal.SIGTERM, interrupt)
    signal.signal(signal.SIGALRM, interrupt)
    signal.alarm(45)
    try:
        daemon = preflight()
        result = check_camera(daemon, args.save_image) if args.check == "camera" else check_audio()
        print(json.dumps(result, indent=2), flush=True)
    finally:
        signal.alarm(0)


if __name__ == "__main__":
    main()
