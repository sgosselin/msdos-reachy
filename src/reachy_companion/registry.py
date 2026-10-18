"""Explicit plugin declarations and local validation, independent of both SDKs."""

import asyncio
import hashlib
import inspect
import math
import re
from dataclasses import dataclass, field
from types import MappingProxyType

from .protocol import CLOUD_LIMIT, Fault, encoded

RESERVED = {"device.health", "system.run", "file.read", "file.write"}
TYPES = {"string": str, "integer": int, "number": (int, float), "boolean": bool}


@dataclass(frozen=True)
class Parameter:
    type: str
    description: str
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple = ()
    max_length: int = 4096

    def validate(self, value):
        allowed = TYPES.get(self.type)
        if allowed is None or type(value) not in (allowed if isinstance(allowed, tuple) else (allowed,)):
            raise Fault("invalid_params", "parameter_type")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if (not math.isfinite(value) or self.minimum is not None and value < self.minimum
                    or self.maximum is not None and value > self.maximum):
                raise Fault("invalid_params", "parameter_range")
        if isinstance(value, str) and len(value) > self.max_length:
            raise Fault("size_limit", "parameter")
        if self.choices and value not in self.choices:
            raise Fault("invalid_params", "parameter_choice")

    def advertised(self):
        return {"type": self.type, "description": self.description}


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    handler: object
    version: str = "1"
    required: dict = field(default_factory=dict)
    optional: dict = field(default_factory=dict)
    timeout_ms: int = 3000
    result_limit: int = CLOUD_LIMIT - 1024
    concurrency: str = "read"
    cancel: object = None

    def __post_init__(self):
        object.__setattr__(self, "required", MappingProxyType(dict(self.required)))
        object.__setattr__(self, "optional", MappingProxyType(dict(self.optional)))

    def validate(self, params):
        if (not isinstance(params, dict) or set(params) - (self.required.keys() | self.optional.keys())
                or self.required.keys() - params.keys()):
            raise Fault("invalid_params", "arguments")
        for name, value in params.items():
            (self.required.get(name) or self.optional[name]).validate(value)
        return dict(params)

    def advertised(self):
        return {"description": self.description, "required": {k: v.advertised() for k, v in self.required.items()},
                "optional": {k: v.advertised() for k, v in self.optional.items()}, "timeout_ms": self.timeout_ms}


class Registry:
    def __init__(self):
        self.tools = {}
        self.frozen = False

    def add(self, tool):
        if self.frozen:
            raise Fault("invalid_params", "frozen_catalog")
        if (not isinstance(tool, Tool) or not re.fullmatch(r"[a-z][a-z0-9_.]{1,63}", tool.name)
                or tool.name in self.tools or tool.name in RESERVED or not tool.description
                or not tool.version or not inspect.iscoroutinefunction(tool.handler)
                or tool.cancel is not None and not inspect.iscoroutinefunction(tool.cancel)
                or tool.concurrency not in ("read", "media", "motion")
                or not 1 <= tool.timeout_ms <= 120000 or not 1 <= tool.result_limit < CLOUD_LIMIT
                or tool.required.keys() & tool.optional.keys()):
            raise Fault("invalid_params", "tool_definition")
        for name, param in {**tool.required, **tool.optional}.items():
            if (not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", name) or not isinstance(param, Parameter)
                    or param.type not in TYPES or not param.description or param.max_length < 1):
                raise Fault("invalid_params", "parameter_definition")
        self.tools[tool.name] = tool

    def freeze(self):
        self.frozen = True
        self.tools = MappingProxyType(dict(self.tools))
        # Hash all public and local constraints so a changed worker cannot silently reconnect.
        self._catalog = {name: tool.advertised() for name, tool in self.tools.items()}
        metadata = {name: {"spec": tool.advertised(), "version": tool.version,
                          "concurrency": tool.concurrency, "result_limit": tool.result_limit,
                          "params": {k: vars(v) for k, v in {**tool.required, **tool.optional}.items()}}
                    for name, tool in self.tools.items()}
        self.hash = hashlib.sha256(encoded(metadata)).hexdigest()
        if len(encoded(self.catalog())) > 48 * 1024:
            raise Fault("size_limit", "catalog")

    def catalog(self):
        import copy
        return {"hash": self.hash, "commands": copy.deepcopy(self._catalog)}


class Cancellation:
    def __init__(self):
        self.event = asyncio.Event()

    def check(self):
        if self.event.is_set():
            raise asyncio.CancelledError

    def cancel(self):
        self.event.set()
