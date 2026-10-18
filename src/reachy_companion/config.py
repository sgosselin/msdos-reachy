"""Private JSON configuration. Relative paths resolve beside the config file."""

import json
import os
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .protocol import Fault


@dataclass
class Config:
    worker_socket: Path
    adapter_socket: Path
    state_dir: Path
    worker_uid: int = field(default_factory=os.getuid)
    adapter_uid: int = field(default_factory=os.getuid)
    socket_gid: int = field(default_factory=os.getgid)
    modules: tuple = ("reachy_companion.tools.core",)
    audio_enabled: bool = False
    camera_enabled: bool = False
    motion_enabled: bool = False
    checked_presets: tuple = ()
    run_as: str | None = None

    @classmethod
    def load(cls, path):
        path = Path(path)
        try:
            raw = json.loads(path.read_text())
            if raw.pop("version") != 1 or set(raw) - set(cls.__dataclass_fields__):
                raise ValueError
            for key in ("worker_socket", "adapter_socket", "state_dir"):
                raw[key] = path.parent / raw[key]
            for key in ("worker_uid", "adapter_uid", "socket_gid"):
                if key in raw and (type(raw[key]) is not int or raw[key] < 0):
                    raise ValueError
            for key in ("audio_enabled", "camera_enabled", "motion_enabled"):
                if key in raw and type(raw[key]) is not bool:
                    raise ValueError
            modules = raw.get("modules", cls.modules)
            if (not isinstance(modules, (list, tuple)) or not modules or len(modules) > 32
                    or len(set(modules)) != len(modules)
                    or any(not isinstance(m, str) or not re.fullmatch(r"[a-zA-Z_]\w*(\.[a-zA-Z_]\w*)*", m) for m in modules)):
                raise ValueError
            raw["modules"] = tuple(modules)
            presets = raw.get("checked_presets", [])
            if not isinstance(presets, list) or any(p not in ("nod", "curious") for p in presets):
                raise ValueError
            raw["checked_presets"] = tuple(presets)
            if raw.get("run_as") is not None and (not isinstance(raw["run_as"], str)
                    or not re.fullmatch(r"[a-z_][a-z0-9_-]{0,63}", raw["run_as"])):
                raise ValueError
            result = cls(**raw)
            if result.worker_socket.parent.resolve() == result.adapter_socket.parent.resolve():
                raise ValueError
            return result
        except (OSError, ValueError, TypeError, KeyError):
            raise Fault("invalid_params", "configuration") from None


def chat_id(state_dir, *, new=False):
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = state_dir / "chat-id"
    if not new and path.exists():
        try:
            return str(uuid.UUID(path.read_text().strip()))
        except ValueError:
            raise Fault("invalid_params", "chat_state") from None
    value = str(uuid.uuid4())
    temporary = state_dir / (".chat-" + str(uuid.uuid4()))
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as output:
        output.write(value + "\n")
    temporary.replace(path)
    return value
