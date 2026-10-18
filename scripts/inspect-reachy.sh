#!/usr/bin/env bash
# Step 1.1: run on Reachy's Linux computer, directly or through SSH stdin.
# Reads inventory/status only. No sudo, installs, SDK instances or media capture.
# API reference: https://huggingface.co/docs/reachy_mini/en/API/rest-api
set -u

if [ "$(uname -s)" != Linux ]; then
    printf 'Run this script on Reachy over SSH, not on your Mac.\n' >&2
    exit 1
fi

printf '\nHost and operating system\n'
hostname
id -un
hostname -I
uname -m
if [ -r /etc/os-release ]; then cat /etc/os-release; fi
if [ -r /proc/device-tree/model ]; then
    tr -d '\000' < /proc/device-tree/model
    printf '\n'
fi

printf '\nService states\n'
for unit in reachy-mini-daemon.service musegadget.service; do
    timeout 5 systemctl show "$unit" --no-pager \
        --property=Id,LoadState,ActiveState,SubState,MainPID || true
done

printf '\nPython and installed package versions\n'
for interpreter in /usr/bin/python3 /venvs/*/bin/python /opt/musegadget/venv/bin/python; do
    [ -x "$interpreter" ] || continue
    timeout 5 "$interpreter" -B - <<'PY'
import sys
from importlib.metadata import PackageNotFoundError, version
print("Interpreter:", sys.executable, sys.version.split()[0])
for name in ("reachy-mini", "musegadget"):
    try:
        print(" ", name, version(name))
    except PackageNotFoundError:
        print(" ", name, "not installed in this environment")
PY
done

printf '\nReachy daemon and current managed app\n'
python3 -B - <<'PY'
import json
from urllib.request import ProxyHandler, build_opener

opener = build_opener(ProxyHandler({}))
for path, fields in (
    ("/api/daemon/status", ("state", "wireless_version", "simulation_enabled")),
    ("/api/apps/current-app-status", ("state",)),
):
    try:
        with opener.open("http://127.0.0.1:8000" + path, timeout=3) as response:
            raw = response.read(65537)
        if len(raw) > 65536:
            raise ValueError("status response too large")
        data = json.loads(raw)
        if data is None:
            print(path, "no current managed app")
        elif isinstance(data, dict):
            selected = {key: data[key] for key in fields if key in data}
            if isinstance(data.get("info"), dict):
                selected["app_name"] = data["info"].get("name")
            print(path, json.dumps(selected))
        else:
            print(path, "unexpected response shape")
    except Exception as exc:
        print(path, "unavailable:", type(exc).__name__, str(exc))
PY

printf '\nEnumerated audio and camera devices (not a capture test)\n'
if [ -r /proc/asound/cards ]; then cat /proc/asound/cards; fi
for camera_name in /sys/class/video4linux/*/name; do
    [ -r "$camera_name" ] || continue
    printf '%s: ' "$camera_name"
    cat "$camera_name"
done

printf '\nMuse command and local socket\n'
command -v musegadget || true
if [ -S /run/musegadget/musegadget.sock ]; then
    printf 'Muse local socket exists; pairing and live registration still need verification.\n'
else
    printf 'Muse local socket absent or inaccessible.\n'
fi
printf '\nInventory finished. No functional media or motion tests were performed.\n'
