"""Policy-bounded shared cache over the same atomic state port.

No store is wired to production. Expired records are deleted on access; a
deployment additionally needs verified proactive TTL/deletion enforcement.
"""

import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from app.ports.atomic_state import AtomicStateStore
from app.sources.catalog import source_capability

_HASH = re.compile(r"^[a-f0-9]{64}$")
_FORBIDDEN = frozenset({"api_key", "apikey", "access_token", "token", "authorization", "password",
                        "secret", "credential", "raw_response"})
# Wikimedia's official top-list is bounded to a finite daily result. Allow its
# complete normalized payload to be cached while retaining a strict byte ceiling.
_MAX_BYTES = 4_000_000
_SECRET_IN_TEXT = re.compile(r"(?:api[_-]?key|access[_-]?token|authorization|password|secret|token)\s*=", re.I)


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timezone-aware timestamp required")
    return value.astimezone(timezone.utc)


def _safe_payload(value: Any, depth: int = 0) -> None:
    if depth > 12:
        raise ValueError("cache payload nesting limit exceeded")
    if isinstance(value, dict):
        if len(value) > 1000:
            raise ValueError("cache payload object too large")
        for key, child in value.items():
            normalized = key.casefold().replace("-", "_") if isinstance(key, str) else ""
            if (not normalized or normalized in _FORBIDDEN
                    or normalized.endswith(("_api_key", "_access_token", "_password", "_secret"))):
                raise ValueError("invalid or sensitive cache field")
            _safe_payload(child, depth + 1)
    elif isinstance(value, list):
        if len(value) > 5000:
            raise ValueError("cache payload list too large")
        for child in value:
            _safe_payload(child, depth + 1)
    elif isinstance(value, str) and _SECRET_IN_TEXT.search(value):
        raise ValueError("sensitive value is not cacheable")
    elif isinstance(value, str) and value.startswith(("https://", "http://")):
        try:
            url = urlsplit(value)
            if (url.username is not None or url.password is not None
                    or any(key.casefold().replace("-", "_") in _FORBIDDEN
                           for key, _ in parse_qsl(url.query, max_num_fields=100))):
                raise ValueError("sensitive URL is not cacheable")
        except ValueError:
            raise ValueError("invalid or sensitive cache URL") from None
    elif type(value) is float and not math.isfinite(value):
        raise ValueError("non-finite cache value")
    elif value is not None and type(value) not in {str, int, float, bool}:
        raise ValueError("cache payload must be JSON data")


@dataclass(frozen=True)
class CacheScope:
    source_id: str
    provider_id: str
    query: str
    plan_fingerprint: str
    market: str
    language: str
    window: str
    parser_version: str
    policy_version: str

    def __post_init__(self):
        capability = source_capability(self.source_id)
        if (capability.provider_id != self.provider_id or not isinstance(self.plan_fingerprint, str)
                or not _HASH.fullmatch(self.plan_fingerprint)):
            raise ValueError("invalid source, provider or plan")
        for value in (self.query, self.market, self.language, self.window,
                      self.parser_version, self.policy_version):
            if not isinstance(value, str) or not value or len(value) > 256 or "\x00" in value:
                raise ValueError("invalid cache scope")

    @property
    def key(self) -> str:
        payload = json.dumps(asdict(self), sort_keys=True, ensure_ascii=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("ascii")).hexdigest()


@dataclass(frozen=True)
class CacheGrant:
    source_id: str
    policy_version: str
    expires_at: datetime
    max_fresh_seconds: int
    max_stale_seconds: int
    max_retention_seconds: int
    allowed: bool = False

    def __post_init__(self):
        _utc(self.expires_at)
        if (type(self.allowed) is not bool or not isinstance(self.source_id, str)
                or not isinstance(self.policy_version, str) or not self.policy_version):
            raise ValueError("invalid cache grant")
        for value in (self.max_fresh_seconds, self.max_stale_seconds, self.max_retention_seconds):
            if type(value) is not int or not 0 <= value <= 31_536_000:
                raise ValueError("invalid cache duration")
        if not 0 < self.max_fresh_seconds <= self.max_stale_seconds <= self.max_retention_seconds:
            raise ValueError("cache durations must be ordered and positive")


class SharedCache:
    def __init__(self, store: AtomicStateStore, *, payload_blobs=None,
                 max_inline_bytes: int | None = None):
        if not callable(getattr(store, "transact", None)):
            raise TypeError("atomic state store required")
        if payload_blobs is not None and not all(
                callable(getattr(payload_blobs, method, None)) for method in ("put", "get")):
            raise TypeError("payload blob store must support durable reads and writes")
        if max_inline_bytes is None:
            max_inline_bytes = _MAX_BYTES if payload_blobs is None else 500_000
        if type(max_inline_bytes) is not int or not 1 <= max_inline_bytes <= _MAX_BYTES:
            raise ValueError("cache inline payload limit is invalid")
        self.store = store
        self.payload_blobs = payload_blobs
        self.max_inline_bytes = max_inline_bytes

    @staticmethod
    def _authorized(scope: CacheScope, grant: CacheGrant | None, now: datetime) -> bool:
        return bool(isinstance(grant, CacheGrant) and grant.allowed is True
                    and grant.source_id == scope.source_id and grant.policy_version == scope.policy_version
                    and _utc(grant.expires_at) > now)

    def put(self, scope: CacheScope, *, grant: CacheGrant, origin: str, payload: dict,
            fetched_at: datetime, now: datetime) -> None:
        now, fetched_at = _utc(now), _utc(fetched_at)
        if not isinstance(scope, CacheScope) or not self._authorized(scope, grant, now):
            raise PermissionError("cache policy is not approved")
        if origin not in {"SYNTHETIC", "LIVE"} or fetched_at > now or not isinstance(payload, dict):
            raise ValueError("invalid cache data or origin")
        _safe_payload(payload)
        try:
            encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False,
                                 separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError, UnicodeError):
            raise ValueError("cache payload must be finite UTF-8 JSON") from None
        if len(encoded) > _MAX_BYTES:
            raise ValueError("cache payload exceeds size budget")
        # Older results cannot refresh their own age by being reinserted.
        fresh_until = fetched_at + timedelta(seconds=grant.max_fresh_seconds)
        stale_until = fetched_at + timedelta(seconds=grant.max_stale_seconds)
        retain_until = min(fetched_at + timedelta(seconds=grant.max_retention_seconds),
                           _utc(grant.expires_at))
        if now >= retain_until or now >= stale_until:
            raise ValueError("cache entry already expired")
        external = len(encoded) > self.max_inline_bytes
        if external and self.payload_blobs is None:
            raise ValueError("cache payload exceeds inline storage budget")
        payload_ref = None
        if external:
            payload_ref = self.payload_blobs.put(
                [json.loads(encoded)], now=now, retain_until=retain_until
            )
            if not self._valid_payload_reference(payload_ref):
                raise ValueError("cache blob store returned an invalid reference")
        record = {"schema": 2 if external else 1, "scope_hash": scope.key, "origin": origin,
                  "fetched_at": fetched_at.isoformat(),
                  "fresh_until": min(fresh_until, retain_until).isoformat(),
                  "stale_until": min(stale_until, retain_until).isoformat(),
                  "retain_until": retain_until.isoformat(), "last_checked_at": now.isoformat()}
        if external:
            record.update(payload_storage="EXTERNAL_RECORDS", payload_ref=payload_ref)
        else:
            record["payload"] = json.loads(encoded)
        def op(old):
            # A late old result cannot overwrite a newer observation.
            if old:
                if now < _utc(datetime.fromisoformat(old["last_checked_at"])):
                    raise ValueError("cache clock regressed")
                previous = _utc(datetime.fromisoformat(old["fetched_at"]))
                if previous > fetched_at:
                    raise ValueError("cache entry is older than stored data")
                if previous == fetched_at:
                    if old.get("origin") != origin or old.get("payload") != record["payload"]:
                        raise ValueError("conflicting cache entry for same observation")
                    for field in ("fresh_until", "stale_until", "retain_until"):
                        old[field] = min(_utc(datetime.fromisoformat(old[field])),
                                         _utc(datetime.fromisoformat(record[field]))).isoformat()
                    old["last_checked_at"] = now.isoformat()
                    return old, None
            return record, None
        self.store.transact(scope.key, op)

    def get(self, scope: CacheScope, *, grant: CacheGrant | None, now: datetime) -> dict:
        now = _utc(now)
        if not isinstance(scope, CacheScope):
            raise ValueError("cache scope required")
        def op(record):
            if not record:
                return {}, {"status": "UNAVAILABLE", "reason": "CACHE_MISS"}
            if (type(record.get("schema")) is not int or record["schema"] not in {1, 2}
                    or record.get("scope_hash") != scope.key):
                return record, {"status": "REJECTED", "reason": "INVALID_CACHE_RECORD"}
            try:
                fetched = _utc(datetime.fromisoformat(record["fetched_at"]))
                retention = _utc(datetime.fromisoformat(record["retain_until"]))
                stale = _utc(datetime.fromisoformat(record["stale_until"]))
                fresh = _utc(datetime.fromisoformat(record["fresh_until"]))
                checked = _utc(datetime.fromisoformat(record["last_checked_at"]))
                if record["origin"] not in {"LIVE", "SYNTHETIC"}:
                    raise ValueError("invalid cached payload")
                if record["schema"] == 1:
                    if "payload_ref" in record or not isinstance(record.get("payload"), dict):
                        raise ValueError("invalid inline cached payload")
                    _safe_payload(record["payload"])
                elif (self.payload_blobs is None
                      or record.get("payload_storage") != "EXTERNAL_RECORDS"
                      or "payload" in record or not self._valid_payload_reference(
                          record.get("payload_ref"))):
                    raise ValueError("invalid external cached payload")
            except (KeyError, TypeError, ValueError):
                return record, {"status": "REJECTED", "reason": "INVALID_CACHE_RECORD"}
            if not fetched <= fresh <= stale <= retention or fetched > now or now < checked:
                return record, {"status": "REJECTED", "reason": "INVALID_CACHE_TIME"}
            record["last_checked_at"] = now.isoformat()
            if now >= retention:
                return {}, {"status": "EXPIRED", "reason": "RETENTION_EXPIRED"}
            if not self._authorized(scope, grant, now):
                if (isinstance(grant, CacheGrant) and grant.source_id == scope.source_id
                        and grant.policy_version == scope.policy_version
                        and (not grant.allowed or _utc(grant.expires_at) <= now)):
                    return {}, {"status": "UNAVAILABLE", "reason": "POLICY_REVOKED"}
                return record, {"status": "UNAVAILABLE", "reason": "POLICY_UNAVAILABLE"}
            # A newly restricted grant can shorten, never extend, existing retention.
            restricted_retention = min(retention, fetched + timedelta(seconds=grant.max_retention_seconds),
                                       _utc(grant.expires_at))
            restricted_stale = min(stale, fetched + timedelta(seconds=grant.max_stale_seconds),
                                   restricted_retention)
            restricted_fresh = min(fresh, fetched + timedelta(seconds=grant.max_fresh_seconds),
                                   restricted_stale)
            if now >= restricted_retention:
                return {}, {"status": "EXPIRED", "reason": "RETENTION_EXPIRED"}
            record.update(fresh_until=restricted_fresh.isoformat(),
                          stale_until=restricted_stale.isoformat(),
                          retain_until=restricted_retention.isoformat())
            if now >= restricted_stale:
                return record, {"status": "EXPIRED", "reason": "STALE_WINDOW_EXPIRED"}
            result = {"status": "CACHED" if now < restricted_fresh else "STALE",
                      "origin": record["origin"], "fetched_at": record["fetched_at"],
                      "retain_until": restricted_retention.isoformat()}
            if record["schema"] == 1:
                result["payload"] = record["payload"]
            else:
                result["_payload_ref"] = record["payload_ref"]
            return record, result
        result = self.store.transact(scope.key, op)
        payload_ref = result.pop("_payload_ref", None)
        if payload_ref is not None:
            try:
                records = self.payload_blobs.get(payload_ref, now=now)
                if not isinstance(records, list) or len(records) != 1 or not isinstance(records[0], dict):
                    raise ValueError("cache payload blob shape is invalid")
                _safe_payload(records[0])
            except (KeyError, TypeError, ValueError, UnicodeError):
                return {"status": "UNAVAILABLE", "reason": "CACHE_BLOB_UNAVAILABLE"}
            result["payload"] = records[0]
        return result

    @staticmethod
    def _valid_payload_reference(reference) -> bool:
        return bool(
            isinstance(reference, dict)
            and isinstance(reference.get("digest"), str)
            and _HASH.fullmatch(reference["digest"])
            and type(reference.get("raw_bytes")) is int
            and 0 <= reference["raw_bytes"] <= _MAX_BYTES + 16
            and type(reference.get("item_count")) is int
            and reference["item_count"] == 1
        )

    def delete(self, scope: CacheScope) -> None:
        if not isinstance(scope, CacheScope):
            raise ValueError("cache scope required")
        self.store.transact(scope.key, lambda _record: ({}, None))
