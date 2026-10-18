"""Bounded daemon probes and cancellable child processes for blocking device APIs."""

import asyncio
import base64
import contextlib
import json
import signal
import sys
from urllib.request import ProxyHandler, build_opener

from ..protocol import Fault, encoded


def daemon_get(path):
    # Fixed loopback endpoint; never forwards arbitrary URLs or honours HTTP proxies.
    with build_opener(ProxyHandler({})).open("http://127.0.0.1:8000" + path, timeout=1) as response:
        data = response.read(65537)
    if len(data) > 65536:
        raise Fault("size_limit", "daemon_status")
    return json.loads(data)


def preflight(*, media=True):
    from importlib.metadata import version
    if version("reachy-mini") != "1.9.0":
        raise Fault("not_ready", "reachy_sdk_version")
    daemon = daemon_get("/api/daemon/status")
    if daemon_get("/api/apps/current-app-status") is not None:
        raise Fault("busy", "managed_app")
    if daemon.get("state") != "running" or daemon.get("error"):
        raise Fault("not_ready", "daemon")
    if media and (daemon.get("no_media") or daemon.get("media_released")):
        raise Fault("not_ready", "daemon_media")
    return daemon


class Hardware:
    def __init__(self, config):
        self.config = config
        self.children = set()
        self.stop_confirmed = True

    async def status(self):
        def probe():
            try:
                daemon = daemon_get("/api/daemon/status")
                app = daemon_get("/api/apps/current-app-status")
                return {"daemon": daemon.get("state", "unknown"), "managed_app_active": app is not None,
                        "media_available": daemon.get("state") == "running" and not daemon.get("error")
                            and not daemon.get("no_media") and not daemon.get("media_released") and app is None}
            except Exception:
                return {"daemon": "unavailable", "media_available": None, "managed_app_active": None}
        observed = await asyncio.to_thread(probe)  # Read-only and bounded network I/O.
        return {**observed, "audio_enabled": self.config.audio_enabled, "camera_enabled": self.config.camera_enabled,
                "motion_enabled": self.config.motion_enabled, "checked_presets": list(self.config.checked_presets),
                "physical_validation": "operator_confirmation_required"}

    async def _terminate(self, proc):
        if proc.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError):
            proc.send_signal(signal.SIGTERM)
        try:
            await asyncio.wait_for(proc.wait(), 2)
        except TimeoutError:
            self.stop_confirmed = False
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()

    async def _run(self, op, payload, timeout):
        proc = await asyncio.create_subprocess_exec(sys.executable, "-m", "reachy_companion.hardware.child", op,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            limit=12 * 1024 * 1024)
        self.children.add(proc)
        communication = asyncio.create_task(proc.communicate(encoded(payload)))
        try:
            async with asyncio.timeout(timeout):
                output, _ = await asyncio.shield(communication)
            if len(output) > 12 * 1024 * 1024:
                raise Fault("size_limit", "hardware_result")
            result = json.loads(output)
            if "error" in result:
                raise Fault(result["error"]["code"], result["error"]["stage"])
            if proc.returncode != 0:
                raise Fault("not_ready", "hardware_child")
            return result
        except (asyncio.CancelledError, TimeoutError):
            await self._terminate(proc)
            raise
        except (ValueError, KeyError):
            raise Fault("not_ready", "hardware_child") from None
        finally:
            if proc.returncode is None:
                await self._terminate(proc)
            await asyncio.gather(communication, return_exceptions=True)
            self.children.discard(proc)

    async def record(self, seconds):
        if not self.config.audio_enabled:
            raise Fault("not_ready", "audio_gate")
        result = await self._run("record", {"seconds": seconds}, seconds + 35)
        return base64.b64decode(result["wav"])

    async def play(self, mp3):
        if not self.config.audio_enabled:
            raise Fault("not_ready", "audio_gate")
        return await self._run("play", {"mp3": base64.b64encode(mp3).decode()}, 100)

    async def camera(self, size, quality):
        if not self.config.camera_enabled:
            raise Fault("not_ready", "camera_gate")
        # Reachy's SDK imports its vision stack even for a camera-only child.
        return await self._run("camera", {"size": size, "quality": quality}, 38)

    async def emote(self, preset, intensity):
        if not self.config.motion_enabled or preset not in self.config.checked_presets:
            raise Fault("not_ready", "motion_gate")
        return await self._run("emote", {"preset": preset, "intensity": intensity}, 38)

    async def stop(self):
        children = list(self.children)
        await asyncio.gather(*(self._terminate(p) for p in children))
        return {"local_work_stopped": bool(children), "child_processes_stopped": all(p.returncode is not None for p in children),
                "forced_kill_required": not self.stop_confirmed, "physical_motion_stop_confirmed": False}
