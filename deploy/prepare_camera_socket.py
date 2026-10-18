#!/usr/bin/env python3
"""Grant the companion group access to an existing daemon-owned camera socket."""

import argparse
import json
import os
import stat
import time
from pathlib import Path
from urllib.request import ProxyHandler, build_opener


def prepare(path, owner, group):
    info = path.lstat()
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != owner:
        raise ValueError("Camera endpoint must be a socket owned by the configured daemon account")
    os.chown(path, -1, group, follow_symlinks=False)
    os.chmod(path, 0o660, follow_symlinks=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", type=Path, required=True)
    parser.add_argument("--owner", type=int, required=True)
    parser.add_argument("--group", type=int, required=True)
    args = parser.parse_args()
    opener = build_opener(ProxyHandler({}))
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        try:
            with opener.open("http://127.0.0.1:8000/api/daemon/status", timeout=1) as response:
                state = json.loads(response.read(65536))
            if state.get("state") == "running" and not state.get("no_media") and not state.get("media_released"):
                prepare(args.socket, args.owner, args.group)
                return
        except (FileNotFoundError, ConnectionError, OSError):
            pass
        time.sleep(.5)
    parser.exit(1, "Camera socket was not ready within the startup deadline.\n")


if __name__ == "__main__":
    main()
