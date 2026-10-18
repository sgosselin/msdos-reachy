"""Small state-change cues, enabled only for operator-checked presets."""

import asyncio

from ..protocol import Fault

_task = None


def register(registry):
    pass


async def start(context):
    global _task
    async def observe():
        previous, cue = None, None
        try:
            while True:
                state = context.state
                if state != previous:
                    if cue:
                        cue.cancel()
                        await asyncio.gather(cue, return_exceptions=True)
                        cue = None
                    preset = {"listening": "curious", "thinking": "curious", "speaking": "nod"}.get(state)
                    if context.config.motion_enabled and preset in context.config.checked_presets:
                        async def gesture(preset=preset):
                            try:
                                await context.invoke("reachy.emote", {"preset": preset, "intensity": .25})
                            except Fault:
                                pass
                        cue = context.spawn(gesture())
                    previous = state
                await asyncio.sleep(.05)
        finally:
            if cue:
                cue.cancel()
                await asyncio.gather(cue, return_exceptions=True)
    _task = context.spawn(observe())


async def stop():
    global _task
    if _task:
        _task.cancel()
        await asyncio.gather(_task, return_exceptions=True)
        _task = None
