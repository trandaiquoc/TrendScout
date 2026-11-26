"""Lossless JSON-compatible encoding of analysis document timestamps.

Firestore timestamps can contain nanoseconds, while PostgreSQL timestamps and
Python datetime normally preserve microseconds. Timestamp paths are therefore
stored separately from the per-record JSON payload; no field name is reserved.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


class AnalysisTimestamp(datetime):
    """A datetime that preserves Firestore's sub-microsecond timestamp digits."""

    __slots__ = ("_nanosecond",)

    def __new__(cls, *args, nanosecond=0, **kwargs):
        if type(nanosecond) is not int or not 0 <= nanosecond < 1_000_000_000:
            raise ValueError("timestamp nanoseconds are out of range")
        if nanosecond:
            if "microsecond" in kwargs:
                raise TypeError("Specify only one of 'microsecond' or 'nanosecond'")
            kwargs["microsecond"] = nanosecond // 1000
        instance = datetime.__new__(cls, *args, **kwargs)
        instance._nanosecond = nanosecond
        return instance

    @property
    def nanosecond(self):
        return self._nanosecond


def analysis_record_key(run_id, kind, record_id):
    raw = json.dumps([run_id, kind, record_id], ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def encode_document(document):
    if not isinstance(document, dict):
        raise ValueError("analysis document must be a mapping")
    timestamps = []

    def encode(value, path):
        if isinstance(value, datetime):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("analysis timestamp must have a timezone")
            utc = value.astimezone(timezone.utc)
            delta = utc.replace(microsecond=0) - _EPOCH
            seconds = delta.days * 86400 + delta.seconds
            nanos = utc.microsecond * 1000 + getattr(value, "nanosecond", value.microsecond * 1000) % 1000
            timestamps.append({"path": path, "seconds": seconds, "nanoseconds": nanos})
            return None
        if isinstance(value, dict):
            if any(not isinstance(key, str) or "\x00" in key for key in value):
                raise ValueError("analysis document contains an invalid field name")
            return {key: encode(child, [*path, key]) for key, child in value.items()}
        if isinstance(value, list):
            return [encode(child, [*path, index]) for index, child in enumerate(value)]
        if value is None or type(value) in (str, int, float, bool):
            return value
        raise ValueError("analysis document contains an unsupported value")

    try:
        payload = encode(document, [])
        json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, UnicodeError, RecursionError):
        raise ValueError("analysis document must be finite UTF-8 data") from None
    timestamps.sort(key=lambda item: json.dumps(item["path"], ensure_ascii=False, separators=(",", ":")))
    return payload, timestamps


def decode_document(payload, timestamps):
    # Round-trip through JSON for an isolated copy and validation of stored data.
    try:
        result = json.loads(json.dumps(payload, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError, UnicodeError):
        raise ValueError("stored analysis document is invalid") from None
    if not isinstance(result, dict) or not isinstance(timestamps, list):
        raise ValueError("stored analysis document is invalid")
    seen = set()
    for item in timestamps:
        if (not isinstance(item, dict) or set(item) != {"path", "seconds", "nanoseconds"}
                or not isinstance(item["path"], list) or not item["path"]
                or type(item["seconds"]) is not int or type(item["nanoseconds"]) is not int
                or not 0 <= item["nanoseconds"] < 1_000_000_000):
            raise ValueError("stored analysis timestamp metadata is invalid")
        path = item["path"]
        if any(not (isinstance(part, str) or type(part) is int and part >= 0) for part in path):
            raise ValueError("stored analysis timestamp path is invalid")
        key = tuple(path)
        if key in seen:
            raise ValueError("stored analysis timestamp path is duplicated")
        seen.add(key)
        node = result
        try:
            for part in path[:-1]:
                if isinstance(node, list) and type(part) is not int:
                    raise TypeError()
                node = node[part]
            last = path[-1]
            if isinstance(node, list) and type(last) is not int:
                raise TypeError()
            if node[last] is not None:
                raise ValueError("timestamp path does not point to its placeholder")
            base = _EPOCH + timedelta(seconds=item["seconds"])
            node[last] = AnalysisTimestamp(
                base.year, base.month, base.day, base.hour, base.minute, base.second,
                nanosecond=item["nanoseconds"], tzinfo=timezone.utc,
            )
        except (KeyError, IndexError, TypeError, OverflowError):
            raise ValueError("stored analysis timestamp path is invalid") from None
    return result


def created_sort_key(payload, timestamps):
    """Return an indexable Firestore-order key for the run's created_at field.

    Missing fields are excluded by Firestore order_by. Complex types are kept
    but marked unsupported for ordered reads instead of inventing an order.
    """
    empty = (0, 0, 0, b"")
    if "created_at" not in payload:
        return (None, *empty)
    stamp = next((item for item in timestamps if item["path"] == ["created_at"]), None)
    if stamp is not None:
        return (4, 0, stamp["seconds"], stamp["nanoseconds"], b"")
    value = payload["created_at"]
    if value is None:
        return (1, *empty)
    if type(value) is bool:
        return (2, int(value), 0, 0, b"")
    if type(value) is int:
        return (3, value, 0, 0, b"")
    if type(value) is float:
        return (3, Decimal.from_float(value), 0, 0, b"")
    if type(value) is str:
        return (5, 0, 0, 0, value.encode("utf-8")[:1500])
    return (6, *empty)
