"""Pure conversions, shared with tests; no device access."""

import base64
import io
import wave
from datetime import datetime, timezone

from ..protocol import CLOUD_LIMIT, WAV_LIMIT, Fault, encoded


def pcm_wav(samples, rate=16000):
    import numpy as np
    samples = np.asarray(samples)
    if (samples.ndim != 2 or samples.shape[1] not in (1, 2) or rate != 16000
            or not 0 < len(samples) <= rate * 20 or not np.isfinite(samples).all()):
        raise Fault("invalid_params", "microphone_format")
    mono = np.clip(samples.mean(axis=1), -1, 1)
    pcm = np.rint(mono * 32767).astype("<i2")
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(pcm.tobytes())
    return output.getvalue()


def validate_wav(data):
    if not 0 < len(data) <= WAV_LIMIT:
        raise Fault("size_limit", "wav")
    try:
        with wave.open(io.BytesIO(data), "rb") as wav:
            if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate(), wav.getcomptype()) != (1, 2, 16000, "NONE"):
                raise ValueError
            count = wav.getnframes()
            if not 0 < count <= 20 * 16000 or len(wav.readframes(count)) != count * 2:
                raise ValueError
    except (ValueError, EOFError, wave.Error):
        raise Fault("invalid_params", "wav_format") from None


def jpeg_result(frame, size=640, quality=75, captured_at=None):
    import numpy as np
    from PIL import Image
    if frame.ndim != 3 or frame.shape[2] != 3 or frame.dtype != np.uint8 or not frame.size:
        raise Fault("invalid_params", "camera_format")
    image = Image.fromarray(frame[:, :, ::-1])  # Reachy produces BGR.
    image.thumbnail((size, size))
    timestamp = captured_at or datetime.now(timezone.utc).isoformat()
    while min(image.size) >= 80:
        output = io.BytesIO()
        image.save(output, format="JPEG", quality=quality)
        result = {"format": "jpeg-base64", "data_base64": base64.b64encode(output.getvalue()).decode(),
                  "captured_at": timestamp, "width": image.width, "height": image.height}
        if len(encoded({"ok": True, "payload": result})) < CLOUD_LIMIT - 1024:
            return result
        if quality > 35:
            quality -= 10
        else:
            image.thumbnail((int(image.width * .8), int(image.height * .8)))
    raise Fault("size_limit", "camera_result")
