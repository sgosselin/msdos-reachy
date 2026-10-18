"""Strict, bounded JSON and connection-owned media transfers."""

import base64
import json
import math
import time
import uuid
from dataclasses import dataclass, field

VERSION = 1
LINE_LIMIT = 64 * 1024
CHUNK = 16 * 1024
CLOUD_LIMIT = 240 * 1024
WAV_LIMIT = 2 * 1024 * 1024
MP3_LIMIT = 8 * 1024 * 1024
TRANSFER_LIMITS = {"voice": ("audio/wav", WAV_LIMIT),
                   "speech": ("audio/mpeg", MP3_LIMIT),
                   "tool-result": ("application/json", CLOUD_LIMIT)}


class Fault(Exception):
    def __init__(self, code, stage="request"):
        # Deliberately never include arbitrary exception messages or parameters.
        self.code, self.stage = code, stage
        super().__init__(f"{code}: {stage}")

    def as_dict(self):
        return {"code": self.code, "stage": self.stage}


def encoded(value):
    try:
        return json.dumps(value, ensure_ascii=True, allow_nan=False,
                          separators=(",", ":"), sort_keys=True).encode()
    except (ValueError, TypeError, RecursionError):
        raise Fault("invalid_params", "json") from None


def line(value):
    data = encoded(value) + b"\n"
    if len(data) > LINE_LIMIT:
        raise Fault("size_limit", "json_line")
    return data


def decode(data, *, limit=LINE_LIMIT):
    if len(data) > limit:
        raise Fault("size_limit", "json_line")
    try:
        value = json.loads(data, parse_constant=lambda _: None)
    except (ValueError, UnicodeError, RecursionError):
        raise Fault("invalid_params", "json") from None
    if not isinstance(value, dict):
        raise Fault("invalid_params", "json_object")
    return value


def identifier(value):
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise ValueError
    except (ValueError, AttributeError):
        raise Fault("invalid_params", "identifier") from None
    return value


def request(value):
    identifier(value.get("id"))
    deadline = value.get("deadline")
    if (type(value.get("v")) is not int or value["v"] != VERSION or not isinstance(value.get("op"), str)
            or len(value["op"]) > 64 or not isinstance(value.get("payload"), dict)
            or type(deadline) not in (int, float) or not math.isfinite(deadline)):
        raise Fault("invalid_params")
    remaining = deadline - time.time()
    if remaining <= 0:
        raise Fault("timeout", "deadline")
    if remaining > 180:
        raise Fault("invalid_params", "deadline")
    return remaining


@dataclass
class Transfer:
    owner: object
    purpose: str
    size: int
    data: bytearray = field(default_factory=bytearray)
    complete: bool = False
    read_offset: int = 0
    created: float = field(default_factory=time.monotonic)
    touched: float = field(default_factory=time.monotonic)


class Transfers:
    """All buffers share process-wide quotas; IDs are scoped to a connection."""
    def __init__(self):
        self.items = {}

    def purge(self):
        now = time.monotonic()
        for key, item in list(self.items.items()):
            if now - item.touched > 15 or now - item.created > 120:
                del self.items[key]

    def begin(self, owner, payload):
        self.purge()
        key = identifier(payload.get("transfer_id"))
        purpose, size = payload.get("purpose"), payload.get("total")
        if (purpose not in TRANSFER_LIMITS or type(size) is not int or size <= 0
                or payload.get("type") != TRANSFER_LIMITS[purpose][0]):
            raise Fault("invalid_params", "transfer")
        if size > TRANSFER_LIMITS[purpose][1]:
            raise Fault("size_limit", "transfer")
        if key in self.items:
            raise Fault("invalid_params", "duplicate_transfer")
        if (len(self.items) >= 3 or sum(x.size for x in self.items.values()) + size > 12 * 1024 * 1024
                or purpose in ("voice", "speech") and any(x.purpose == purpose for x in self.items.values())):
            raise Fault("busy", "transfers")
        self.items[key] = Transfer(owner, purpose, size)
        return {"transfer_id": key, "total": size, "purpose": purpose}

    def get(self, owner, key):
        self.purge()
        item = self.items.get(identifier(key))
        if item is None or item.owner is not owner:
            raise Fault("invalid_params", "unknown_transfer")
        item.touched = time.monotonic()
        return item

    def dispatch(self, owner, op, payload):
        if op == "transfer.begin":
            return self.begin(owner, payload)
        key = payload.get("transfer_id")
        item = self.get(owner, key)
        if op == "transfer.abort":
            del self.items[key]
            return {}
        if op == "transfer.chunk":
            value = payload.get("data")
            if not isinstance(value, str) or len(value) > 4 * ((CHUNK + 2) // 3):
                raise Fault("size_limit", "chunk")
            try:
                data = base64.b64decode(value, validate=True)
            except ValueError:
                raise Fault("invalid_params", "base64") from None
            if (item.complete or type(payload.get("offset")) is not int
                    or payload["offset"] != len(item.data) or not data
                    or len(data) > CHUNK or len(item.data) + len(data) > item.size):
                raise Fault("invalid_params", "chunk")
            item.data.extend(data)
            return {"offset": len(item.data)}
        if op == "transfer.end":
            if item.complete or len(item.data) != item.size:
                raise Fault("invalid_params", "partial_transfer")
            item.complete = True
            return {"transfer_id": key}
        if op == "transfer.read":
            if not item.complete or payload.get("offset") != item.read_offset:
                raise Fault("invalid_params", "transfer_read")
            data = item.data[item.read_offset:item.read_offset + CHUNK]
            item.read_offset += len(data)
            return {"data": base64.b64encode(data).decode(), "offset": item.read_offset,
                    "eof": item.read_offset == item.size}
        raise Fault("invalid_params", "operation")

    def take(self, owner, key, purpose):
        item = self.get(owner, key)
        if not item.complete or item.purpose != purpose:
            raise Fault("invalid_params", "transfer_type")
        del self.items[key]
        return bytes(item.data)

    def put(self, owner, purpose, data):
        ref = self.begin(owner, {"transfer_id": str(uuid.uuid4()), "purpose": purpose,
                                "total": len(data), "type": TRANSFER_LIMITS[purpose][0]})
        item = self.items[ref["transfer_id"]]
        item.data.extend(data)
        item.complete = True
        return ref

    def discard(self, owner):
        self.items = {k: v for k, v in self.items.items() if v.owner is not owner}
