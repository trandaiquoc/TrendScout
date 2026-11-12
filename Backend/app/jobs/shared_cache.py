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
_MAX_BYTES = 300_000
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
    def __init__(self, store: AtomicStateStore):
        if not callable(getattr(store, "transact", None)):
            raise TypeError("atomic state store required")
        self.store = store

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
        record = {"schema": 1, "scope_hash": scope.key, "origin": origin,
                  "payload": json.loads(encoded), "fetched_at": fetched_at.isoformat(),
                  "fresh_until": min(fresh_until, retain_until).isoformat(),
                  "stale_until": min(stale_until, retain_until).isoformat(),
                  "retain_until": retain_until.isoformat(), "last_checked_at": now.isoformat()}
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
            if record.get("schema") != 1 or record.get("scope_hash") != scope.key:
                return record, {"status": "REJECTED", "reason": "INVALID_CACHE_RECORD"}
            try:
                fetched = _utc(datetime.fromisoformat(record["fetched_at"]))
                retention = _utc(datetime.fromisoformat(record["retain_until"]))
                stale = _utc(datetime.fromisoformat(record["stale_until"]))
                fresh = _utc(datetime.fromisoformat(record["fresh_until"]))
                checked = _utc(datetime.fromisoformat(record["last_checked_at"]))
                if record["origin"] not in {"LIVE", "SYNTHETIC"} or not isinstance(record["payload"], dict):
                    raise ValueError("invalid cached payload")
                _safe_payload(record["payload"])
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
            return record, {"status": "CACHED" if now < restricted_fresh else "STALE",
                            "origin": record["origin"], "fetched_at": record["fetched_at"],
                            "payload": record["payload"],
                            "retain_until": restricted_retention.isoformat()}
        return self.store.transact(scope.key, op)

    def delete(self, scope: CacheScope) -> None:
        if not isinstance(scope, CacheScope):
            raise ValueError("cache scope required")
        self.store.transact(scope.key, lambda _record: ({}, None))
