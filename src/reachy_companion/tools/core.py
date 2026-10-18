from ..registry import Parameter, Tool


async def status(params, context, cancellation):
    return await context.status()


async def stop_tool(params, context, cancellation):
    return await context.stop()


async def emote(params, context, cancellation):
    cancellation.check()
    return await context.hardware.emote(params["preset"], params.get("intensity", 0.5))


async def camera(params, context, cancellation):
    cancellation.check()
    return await context.hardware.camera(params.get("size", 640), params.get("quality", 75))


def register(registry):
    registry.add(Tool("reachy.status", "Read runtime, Muse connection and observed daemon/media availability.", status))
    registry.add(Tool("reachy.stop", "Stop local recording, speech and gestures. Does not cancel Muse's remote task.",
                      stop_tool, timeout_ms=10000))
    registry.add(Tool("reachy.emote", "Perform a locally checked preset, if motion has been enabled by the operator.",
                      emote, required={"preset": Parameter("string", "Preset: nod or curious.", choices=("nod", "curious"))},
                      optional={"intensity": Parameter("number", "Intensity from 0 to 1; default 0.5.", minimum=0, maximum=1)},
                      timeout_ms=40000, concurrency="motion"))
    registry.add(Tool("reachy.camera.capture", "Capture a fresh JPEG, if enabled; returns jpeg-base64 and capture time.",
                      camera, optional={"size": Parameter("integer", "Maximum edge pixels, 160 to 1280; default 640.", minimum=160, maximum=1280),
                                        "quality": Parameter("integer", "JPEG quality 20 to 90; default 75.", minimum=20, maximum=90)},
                      timeout_ms=40000, concurrency="media"))
