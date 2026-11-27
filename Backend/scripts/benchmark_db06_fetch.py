"""Profile the real PostgreSQL CACHE_ONLY Fetch path for DB06.

Only randomly named ``ts_test_db06_*`` schemas are created or removed. Provider
Provider HTTP clients and OpenRouter calls are blocked while the app runs.
The cached source fixture is synthetic and stored in the isolated PostgreSQL
schema; fixture setup and migrations are excluded from Fetch timings.
"""

from __future__ import annotations

import argparse
import atexit
import hashlib
import inspect
import json
import os
import re
import signal
import statistics
import threading
import time
import traceback
import urllib.request
import uuid
from collections import Counter, defaultdict
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path

import certifi
import httpx
import psycopg
from fastapi.testclient import TestClient
from psycopg import sql

from app.collections import service as collection_service_module
from app.collections.postgres_store import PostgresCollectionStore
from app.collections.source_digest import source_input_digest
from app.google_live_settings import GoogleLiveSettings
from app.jobs.distributed_worker import DistributedFetchWorker
from app.jobs.public_admission import PublicAdmission
from app.jobs.shared_cache import CacheGrant, SharedCache
from app.jobs.source_checkpoints import SourceCheckpointJournal
from app.normal_wikimedia_live_policy import GOOGLE_POLICY_VERSION, GOOGLE_STATE_KEY
from app.orchestration.distributed_public_fetch import DistributedPublicFetch
from app.orchestration.postgres_live_composition import postgres_live_composition
from app.settings import Settings
from app.sources.catalog import DEFAULT_WIKIMEDIA_USER_AGENT, GOOGLE_SOURCE_ID
from app.sources.google_trends.handler import trending_now_cache_scope
from app.sources.google_trends.trending_now import (
    TRENDING_NOW_SOURCE_URL,
    TrendingNowPage,
    normalize_keyword,
    normalize_trending_candidates,
)
from app.sources.google_trends.verification_plan import MAX_VERIFICATION_QUERIES
from app.sources.query_plan import DEFAULT_QUERY_PLAN
from app.storage.postgres_atomic_state import PostgresAtomicStateStore
from app.storage.postgres_config import postgres_environment as read_postgres_environment
from app.storage.postgres_connection import postgres_connection, postgres_pool_stats
from app.storage.postgres_migrations import apply_migrations
from app.storage.postgres_payloads import PostgresSourcePayloads


class Measurement:
    def __init__(self, label: str):
        self.label = label
        self.started_at = time.perf_counter()
        self.stage_totals: dict[str, dict[str, float | int]] = defaultdict(
            lambda: {"calls": 0, "total_seconds": 0.0, "max_seconds": 0.0}
        )
        self.query_counts: Counter[tuple[str, str, str]] = Counter()
        self.query_patterns: dict[str, str] = {}
        self.query_max_seconds: dict[str, float] = defaultdict(float)
        self.connection_count = 0
        self.connection_seconds = 0.0
        self.pool_stats_before = postgres_pool_stats()
        self.pooled_checkouts = 0
        self.pooled_wait_ms = 0
        self.transaction_count = 0
        self.transaction_seconds = 0.0
        self.network_block_count = 0
        self.openrouter_count = 0
        self.counts: Counter[str] = Counter()
        self.diagnostics: list[dict[str, str]] = []


class Instrumentation:
    def __init__(self):
        self.enabled = False
        self.current: Measurement | None = None
        self._thread = threading.local()
        self._lock = threading.Lock()
        self._active_by_thread: dict[int, tuple[float, str]] = {}

    def start(self, label: str) -> Measurement:
        measurement = Measurement(label)
        with self._lock:
            self.current = measurement
        self.enabled = True
        return measurement

    def stop(self) -> Measurement | None:
        self.enabled = False
        measurement = self.current
        if measurement is not None:
            after = postgres_pool_stats()
            delta = {key: after.get(key, 0) - measurement.pool_stats_before.get(key, 0)
                     for key in measurement.pool_stats_before}
            measurement.connection_count += delta.get("connections_num", 0)
            measurement.connection_seconds += delta.get("connections_ms", 0) / 1000
            measurement.pooled_checkouts = delta.get("requests_num", 0)
            measurement.pooled_wait_ms = delta.get("requests_wait_ms", 0)
        return measurement

    @property
    def current_stage(self) -> str:
        with self._lock:
            if not self._active_by_thread:
                return "unattributed"
            return max(self._active_by_thread.values())[1]

    @contextmanager
    def span(self, name: str, *, announce: bool = False):
        if not self.enabled or self.current is None:
            yield
            return
        stack = list(getattr(self._thread, "stack", ()))
        stack.append(name)
        self._thread.stack = stack
        thread_id = threading.get_ident()
        with self._lock:
            self._active_by_thread[thread_id] = (time.perf_counter(), name)
        start = time.perf_counter()
        if announce:
            emit({"event": "stage_start", "measurement": self.current.label, "stage": name})
        try:
            yield
        finally:
            elapsed = time.perf_counter() - start
            stack.pop()
            self._thread.stack = stack
            with self._lock:
                if stack:
                    self._active_by_thread[thread_id] = (time.perf_counter(), stack[-1])
                else:
                    self._active_by_thread.pop(thread_id, None)
            target = self.current.stage_totals[name]
            target["calls"] = int(target["calls"]) + 1
            target["total_seconds"] = float(target["total_seconds"]) + elapsed
            target["max_seconds"] = max(float(target["max_seconds"]), elapsed)
            if announce:
                emit({"event": "stage_end", "measurement": self.current.label,
                      "stage": name, "elapsed_ms": round(elapsed * 1000, 2)})

    def count(self, name: str, value: int = 1) -> None:
        if self.enabled and self.current is not None:
            self.current.counts[name] += value

    def connection_opened(self, elapsed: float) -> None:
        if self.enabled and self.current is not None:
            self.current.connection_count += 1
            self.current.connection_seconds += elapsed

    def transaction_finished(self, elapsed: float) -> None:
        if self.enabled and self.current is not None:
            self.current.transaction_count += 1
            self.current.transaction_seconds += elapsed

    @staticmethod
    def _query_text(query, connection) -> str:
        try:
            if callable(getattr(query, "as_string", None)):
                value = query.as_string(connection)
            elif isinstance(query, bytes):
                value = query.decode("utf-8", "replace")
            else:
                value = str(query)
        except Exception:
            value = type(query).__name__
        value = re.sub(r"'(?:''|[^'])*'", "?", value)
        value = re.sub(r"\b\d+(?:\.\d+)?\b", "?", value)
        return re.sub(r"\s+", " ", value).strip()

    def sql_call(self, connection, kind: str, query, operation):
        if not self.enabled or self.current is None:
            return operation()
        started = time.perf_counter()
        try:
            return operation()
        finally:
            elapsed = time.perf_counter() - started
            pattern = self._query_text(query, connection)
            digest = hashlib.sha256(pattern.encode("utf-8", "replace")).hexdigest()[:16]
            key = (self.current_stage, kind, digest)
            self.current.query_counts[key] += 1
            self.current.query_patterns.setdefault(digest, pattern[:240])
            self.current.query_max_seconds[digest] = max(
                self.current.query_max_seconds[digest], elapsed
            )

    def snapshot(self, measurement: Measurement) -> dict:
        queries = []
        for (stage, kind, digest), count in measurement.query_counts.most_common(12):
            queries.append({
                "stage": stage,
                "kind": kind,
                "count": count,
                "fingerprint": digest,
                "max_client_elapsed_ms": round(
                    measurement.query_max_seconds.get(digest, 0.0) * 1000, 2
                ),
                "sql_template": measurement.query_patterns.get(digest, ""),
            })
        return {
            "measurement": measurement.label,
            "total_elapsed_ms": round((time.perf_counter() - measurement.started_at) * 1000, 2),
            "stages": {
                key: {
                    "calls": value["calls"],
                    "total_ms": round(float(value["total_seconds"]) * 1000, 2),
                    "max_ms": round(float(value["max_seconds"]) * 1000, 2),
                }
                for key, value in sorted(measurement.stage_totals.items())
            },
            "sql": {
                "executions": sum(measurement.query_counts.values()),
                "synchronous_dispatches": sum(measurement.query_counts.values()),
                "dispatch_note": (
                    "Application execute/executemany calls; executemany may be pipelined, "
                    "so this is a client dispatch count rather than TCP packet count."
                ),
                "top_fingerprints": queries,
            },
            "connections": {
                "opened": measurement.connection_count,
                "establishment_ms": round(measurement.connection_seconds * 1000, 2),
                "pooled_checkouts": measurement.pooled_checkouts,
                "pool_wait_ms": measurement.pooled_wait_ms,
            },
            "transactions": {
                "count": measurement.transaction_count,
                "elapsed_ms": round(measurement.transaction_seconds * 1000, 2),
            },
            "provider_network_requests_blocked": measurement.network_block_count,
            "openrouter_requests_blocked": measurement.openrouter_count,
            "counts": dict(measurement.counts),
            "diagnostics": list(measurement.diagnostics),
        }


tracker = Instrumentation()
_owned_schemas: set[str] = set()
_runtime_environment: dict[str, str] | None = None
_test_url: str | None = None


def emit(value: dict) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str), flush=True)


class Heartbeat:
    def __init__(self, interval_seconds: float = 5.0):
        self.interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=2)

    def _run(self):
        while not self._stop.wait(self.interval_seconds):
            if tracker.enabled and tracker.current is not None:
                emit({"event": "heartbeat", "measurement": tracker.current.label,
                      "active_stage": tracker.current_stage,
                      "elapsed_ms": round(
                          (time.perf_counter() - tracker.current.started_at) * 1000, 2
                      ),
                      "connections": tracker.current.connection_count,
                      "sql_dispatches": sum(tracker.current.query_counts.values())})


class _ObservedCursor:
    def __init__(self, connection, cursor):
        self._connection = connection
        self._cursor = cursor

    def execute(self, query, *args, **kwargs):
        return tracker.sql_call(
            self._connection._real, "execute", query,
            lambda: self._cursor.execute(query, *args, **kwargs),
        )

    def executemany(self, query, params_seq, *args, **kwargs):
        try:
            self._connection._real
            if tracker.enabled and tracker.current is not None:
                try:
                    tracker.count("executemany_rows", len(params_seq))
                except (TypeError, AttributeError):
                    pass
        except Exception:
            pass
        return tracker.sql_call(
            self._connection._real, "executemany", query,
            lambda: self._cursor.executemany(query, params_seq, *args, **kwargs),
        )

    def __getattr__(self, name):
        return getattr(self._cursor, name)

    def __enter__(self):
        self._cursor.__enter__()
        return self

    def __exit__(self, *args):
        return self._cursor.__exit__(*args)


class _ObservedTransaction:
    def __init__(self, connection, transaction):
        self._connection = connection
        self._transaction = transaction
        self._started = None

    def __enter__(self):
        self._started = time.perf_counter()
        result = self._transaction.__enter__()
        return self._connection if result is self._connection._real else result

    def __exit__(self, *args):
        try:
            return self._transaction.__exit__(*args)
        finally:
            if self._started is not None:
                tracker.transaction_finished(time.perf_counter() - self._started)


class _ObservedConnection:
    def __init__(self, connection):
        object.__setattr__(self, "_real", connection)

    def execute(self, query, *args, **kwargs):
        return tracker.sql_call(
            self._real, "execute", query,
            lambda: self._real.execute(query, *args, **kwargs),
        )

    def executemany(self, query, params_seq, *args, **kwargs):
        if tracker.enabled and tracker.current is not None:
            try:
                tracker.count("executemany_rows", len(params_seq))
            except (TypeError, AttributeError):
                pass
        return tracker.sql_call(
            self._real, "executemany", query,
            lambda: self._real.executemany(query, params_seq, *args, **kwargs),
        )

    def cursor(self, *args, **kwargs):
        return _ObservedCursor(self, self._real.cursor(*args, **kwargs))

    def transaction(self, *args, **kwargs):
        return _ObservedTransaction(self, self._real.transaction(*args, **kwargs))

    def __getattr__(self, name):
        return getattr(self._real, name)

    def __setattr__(self, name, value):
        if name == "_real":
            object.__setattr__(self, name, value)
        else:
            setattr(self._real, name, value)

    def __enter__(self):
        self._real.__enter__()
        return self

    def __exit__(self, *args):
        return self._real.__exit__(*args)


_psycopg_connect = psycopg.connect


def _observed_connect(*args, **kwargs):
    started = time.perf_counter()
    connection = _psycopg_connect(*args, **kwargs)
    tracker.connection_opened(time.perf_counter() - started)
    return _ObservedConnection(connection)


def _instrument_method(owner, method_name: str, stage: str, *, count_arg=None,
                       count_name: str | None = None):
    original = getattr(owner, method_name)
    if getattr(original, "_db06_instrumented", False):
        return
    if inspect.iscoroutinefunction(original):
        @wraps(original)
        async def async_wrapper(*args, **kwargs):
            with tracker.span(stage):
                if count_arg:
                    tracker.count(count_name or f"{count_arg}_count",
                                  _argument_count(original, args, kwargs, count_arg))
                return await original(*args, **kwargs)
        wrapped = async_wrapper
    else:
        @wraps(original)
        def sync_wrapper(*args, **kwargs):
            with tracker.span(stage):
                if count_arg:
                    tracker.count(count_name or f"{count_arg}_count",
                                  _argument_count(original, args, kwargs, count_arg))
                return original(*args, **kwargs)
        wrapped = sync_wrapper
    wrapped._db06_instrumented = True
    setattr(owner, method_name, wrapped)


def _argument_count(function, args, kwargs, key: str) -> int:
    try:
        value = inspect.signature(function).bind_partial(*args, **kwargs).arguments.get(key)
    except (TypeError, ValueError):
        value = kwargs.get(key)
    if value is None:
        return 0
    try:
        return len(value)
    except TypeError:
        return 0


def install_instrumentation():
    psycopg.connect = _observed_connect
    from psycopg_pool import ConnectionPool
    from starlette.responses import JSONResponse

    if not getattr(ConnectionPool.connection, "_db06_instrumented", False):
        original_pool_connection = ConnectionPool.connection

        @contextmanager
        @wraps(original_pool_connection)
        def observed_pool_connection(self, *args, **kwargs):
            with original_pool_connection(self, *args, **kwargs) as connection:
                yield _ObservedConnection(connection)

        observed_pool_connection._db06_instrumented = True
        ConnectionPool.connection = observed_pool_connection

    import app.storage.postgres_runtime as postgres_runtime_module
    from app.ai.openrouter import OpenRouterAdapter
    from app.collections.service import CollectionService
    from app.jobs.shared_cache import SharedCache
    from app.storage.postgres_atomic_state import PostgresAtomicStateStore
    from app.storage.postgres_payloads import PostgresSourcePayloads

    _instrument_method(PublicAdmission, "initialize_operational_state", "operational_state_initialization")
    _instrument_method(PublicAdmission, "lookup_idempotency", "job_lease_checkpoint_coordination")
    _instrument_method(PublicAdmission, "admit", "job_lease_checkpoint_coordination")
    _instrument_method(PublicAdmission, "claim_job", "job_lease_checkpoint_coordination")
    _instrument_method(PublicAdmission, "claim_next", "job_lease_checkpoint_coordination")
    _instrument_method(PublicAdmission, "heartbeat", "job_lease_checkpoint_coordination")
    _instrument_method(PublicAdmission, "finish", "job_lease_checkpoint_coordination")
    _instrument_method(PublicAdmission, "read_job", "job_lease_checkpoint_coordination")
    _instrument_method(PublicAdmission, "transact_owned", "job_lease_checkpoint_coordination")
    _instrument_method(SourceCheckpointJournal, "load", "job_lease_checkpoint_coordination")
    _instrument_method(SourceCheckpointJournal, "save", "job_lease_checkpoint_coordination")
    _instrument_method(SourceCheckpointJournal, "save_many", "job_lease_checkpoint_coordination")
    _instrument_method(SourceCheckpointJournal, "completed_result", "job_lease_checkpoint_coordination")
    _instrument_method(DistributedPublicFetch, "submit", "fetch_admission")
    _instrument_method(DistributedPublicFetch, "status", "terminal_status_availability")
    _instrument_method(DistributedFetchWorker, "run_once", "job_lease_checkpoint_coordination")

    original_run_once = DistributedFetchWorker.run_once

    @wraps(original_run_once)
    async def measured_run_once(self, *args, **kwargs):
        result = await original_run_once(self, *args, **kwargs)
        if tracker.enabled and tracker.current is not None:
            reason = getattr(self, "_last_cache_reuse_diagnostic", None)
            if isinstance(reason, str):
                tracker.count("metadata_only_reuse_fallbacks")
                tracker.current.diagnostics.append({
                    "kind": "metadata_only_reuse_fallback", "reason": reason,
                })
            elif isinstance(result, dict) and result.get("dataset_reused") is True:
                tracker.count("metadata_only_reuse_hits")
        return result

    measured_run_once._db06_instrumented = True
    DistributedFetchWorker.run_once = measured_run_once
    _instrument_method(SharedCache, "get", "cache_lookup")
    _instrument_method(PostgresAtomicStateStore, "transact", "atomic_state_operations")
    _instrument_method(PostgresAtomicStateStore, "read_many", "atomic_state_operations")
    _instrument_method(CollectionService, "process_fetch", "collection_processing")
    _instrument_method(CollectionService, "_analyze_persisted_keywords", "algorithm_processing")
    _instrument_method(PostgresCollectionStore, "begin_fetch_processing", "collection_processing")
    _instrument_method(PostgresCollectionStore, "persist_cleaned_keywords", "keyword_persistence",
                       count_arg="records", count_name="keyword_persistence_input_records")
    _instrument_method(PostgresCollectionStore, "_persist_source_evidence", "source_evidence_persistence",
                       count_arg="evidence_rows", count_name="source_evidence_input_rows")
    _instrument_method(PostgresCollectionStore, "_link_dataset_evidence",
                       "dataset_evidence_link_persistence",
                       count_arg="evidence_rows", count_name="dataset_evidence_link_input_rows")
    _instrument_method(PostgresCollectionStore, "persist_dataset_snapshot", "dataset_finalization")
    _instrument_method(PostgresCollectionStore, "mark_fetch_model_unavailable", "dataset_finalization")
    _instrument_method(PostgresCollectionStore, "complete_fetch_dataset_snapshot", "dataset_finalization")
    _instrument_method(JSONResponse, "render", "api_serialization")
    _instrument_method(postgres_runtime_module, "verify_postgres_runtime", "runtime_schema_readiness")
    _instrument_method(postgres_runtime_module, "initialize_postgres_runtime_defaults",
                       "runtime_schema_readiness")
    _instrument_method(collection_service_module, "build_source_evidence", "source_evidence_preparation")

    if not getattr(PostgresSourcePayloads.get, "_db06_instrumented", False):
        original_payload_get = PostgresSourcePayloads.get

        @wraps(original_payload_get)
        def observed_payload_get(self, reference, *args, **kwargs):
            with tracker.span("payload_blob_read"):
                tracker.count("payload_blob_read_attempts")
                try:
                    result = original_payload_get(self, reference, *args, **kwargs)
                except Exception:
                    tracker.count("payload_blob_read_failures")
                    raise
                tracker.count("payload_blob_reads")
                raw_bytes = reference.get("raw_bytes") if isinstance(reference, dict) else None
                if type(raw_bytes) is int and raw_bytes >= 0:
                    tracker.count("payload_blob_read_bytes", raw_bytes)
                tracker.count("payload_blob_read_items", len(result))
                return result

        observed_payload_get._db06_instrumented = True
        PostgresSourcePayloads.get = observed_payload_get

    if not getattr(PostgresSourcePayloads.inspect, "_db06_instrumented", False):
        original_payload_inspect = PostgresSourcePayloads.inspect

        @wraps(original_payload_inspect)
        def observed_payload_inspect(self, reference, *args, **kwargs):
            with tracker.span("payload_blob_inspect"):
                tracker.count("payload_blob_inspections")
                return original_payload_inspect(self, reference, *args, **kwargs)

        observed_payload_inspect._db06_instrumented = True
        PostgresSourcePayloads.inspect = observed_payload_inspect

    original_keyword_record = collection_service_module.keyword_record

    @wraps(original_keyword_record)
    def measured_keyword_record(*args, **kwargs):
        started = time.perf_counter()
        try:
            return original_keyword_record(*args, **kwargs)
        finally:
            tracker.count("keyword_normalization_records")
            elapsed = time.perf_counter() - started
            if tracker.enabled and tracker.current is not None:
                item = tracker.current.stage_totals["keyword_normalization"]
                item["calls"] = int(item["calls"]) + 1
                item["total_seconds"] = float(item["total_seconds"]) + elapsed
                item["max_seconds"] = max(float(item["max_seconds"]), elapsed)
    collection_service_module.keyword_record = measured_keyword_record

    original_openrouter_request = OpenRouterAdapter._request

    @wraps(original_openrouter_request)
    def block_openrouter(self, *args, **kwargs):
        if tracker.enabled and tracker.current is not None:
            tracker.current.openrouter_count += 1
        raise RuntimeError("DB06 benchmark blocks OpenRouter requests")
    OpenRouterAdapter._request = block_openrouter

    original_async_send = httpx.AsyncClient.send
    original_sync_send = httpx.Client.send

    async def block_remote_async(self, request, *args, **kwargs):
        if request.url.host not in {"127.0.0.1", "localhost", "testserver"}:
            if tracker.enabled and tracker.current is not None:
                tracker.current.network_block_count += 1
            raise RuntimeError("DB06 benchmark blocks provider network requests")
        return await original_async_send(self, request, *args, **kwargs)

    def block_remote_sync(self, request, *args, **kwargs):
        if request.url.host not in {"127.0.0.1", "localhost", "testserver"}:
            if tracker.enabled and tracker.current is not None:
                tracker.current.network_block_count += 1
            raise RuntimeError("DB06 benchmark blocks provider network requests")
        return original_sync_send(self, request, *args, **kwargs)

    httpx.AsyncClient.send = block_remote_async
    httpx.Client.send = block_remote_sync

    def block_requests(*_args, **_kwargs):
        if tracker.enabled and tracker.current is not None:
            tracker.current.network_block_count += 1
        raise RuntimeError("DB06 benchmark blocks provider network requests")
    try:
        import requests
        requests.sessions.Session.request = block_requests
    except ImportError:
        pass
    urllib.request.urlopen = block_requests

def _test_database_configuration() -> tuple[dict[str, str], str]:
    global _runtime_environment, _test_url
    env_path = Path(__file__).resolve().parents[2] / ".env"
    runtime = read_postgres_environment(os.environ, env_path)
    test_url = os.environ.get("TRENDSCOUT_TEST_POSTGRES_URL")
    if not test_url:
        test_url = runtime.get("DATABASE_URL_UNPOOLED") or runtime.get("DATABASE_URL")
        if not test_url:
            raise RuntimeError("no authorized PostgreSQL test URL is configured")
        os.environ["TRENDSCOUT_TEST_POSTGRES_URL"] = test_url
    _runtime_environment = runtime
    _test_url = test_url
    return runtime, test_url


def _raw_connect(url: str):
    try:
        return _psycopg_connect(
            url,
            sslmode="verify-full",
            sslrootcert=certifi.where(),
            connect_timeout=10,
            prepare_threshold=None,
        )
    except Exception:
        raise RuntimeError("isolated PostgreSQL test connection failed") from None


def _new_schema_name() -> str:
    return "ts_test_db06_" + uuid.uuid4().hex


def _connect_test():
    if not _test_url:
        raise RuntimeError("PostgreSQL test URL is unavailable")
    return _raw_connect(_test_url)


def _seed_operational_readiness(schema: str, environment: dict[str, str], now: datetime) -> None:
    from app.normal_wikimedia_live_policy import normal_google_live_policy

    config = GoogleLiveSettings()
    policy = normal_google_live_policy(
        serpapi_daily_budget=config.serpapi_daily_budget,
        serpapi_monthly_budget=config.serpapi_monthly_budget,
        rss_daily_budget=config.rss_daily_budget,
        rss_monthly_budget=config.rss_monthly_budget,
        fetch_starts_per_hour=config.fetch_starts_per_hour,
        bing_enabled=config.paid_bing_approved,
        bing_daily_budget=config.bing_daily_budget,
        youtube_enabled=True,
    )
    store = PostgresAtomicStateStore(environment, schema=schema)
    admission = PublicAdmission(
        store, policy, state_key=GOOGLE_STATE_KEY, require_quota_evidence=True
    )
    admission.initialize_operational_state(now=now)
    admission.set_serpapi_billing_cycle((now.date() + timedelta(days=15)).isoformat(), now=now)
    for pool in policy.pools:
        for period in ("daily", "monthly"):
            admission.record_quota_window_evidence(
                pool, period, requests=0, cost=0,
                evidence_source="isolated_db06_synthetic_fixture", now=now,
            )


def create_isolated_schema() -> tuple[str, float]:
    if _runtime_environment is None:
        raise RuntimeError("database configuration is unavailable")
    started = time.perf_counter()
    schema = _new_schema_name()
    with _connect_test() as connection:
        connection.autocommit = True
        exists = connection.execute(
            "SELECT EXISTS (SELECT FROM pg_namespace WHERE nspname=%s)", (schema,)
        ).fetchone()[0]
        if exists:
            raise RuntimeError("random test schema collision; no schema was touched")
        try:
            apply_migrations(connection, schema=schema)
            _owned_schemas.add(schema)
            now = datetime.now(timezone.utc)
            _seed_operational_readiness(schema, _runtime_environment, now)
        except BaseException:
            if schema in _owned_schemas:
                drop_owned_schema(schema)
            raise
    return schema, time.perf_counter() - started


def drop_owned_schema(schema: str) -> bool:
    if schema not in _owned_schemas:
        return False
    if not re.fullmatch(r"ts_test_db06_[a-f0-9]{32}", schema):
        raise RuntimeError("refusing to clean a schema outside this benchmark namespace")
    try:
        with _connect_test() as connection:
            connection.autocommit = True
            connection.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
            )
        _owned_schemas.discard(schema)
        return True
    except Exception:
        emit({"event": "schema_cleanup_failed", "schema": schema})
        return False


def cleanup_owned_schemas():
    for schema in tuple(_owned_schemas):
        drop_owned_schema(schema)


atexit.register(cleanup_owned_schemas)


def _fixture_records(count: int, tag: str, revision: int, observed_at: datetime) -> list[dict]:
    raw_rows = []
    for index in range(count):
        keyword = f"DB06 {tag} emerging topic {index:04d}"
        _, normalized = normalize_keyword(keyword)
        identity = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        cluster = hashlib.sha256(f"{tag}:{revision}:{index}".encode()).hexdigest()
        raw_rows.append({
            "query": keyword,
            "canonical_keyword": keyword,
            "canonical_keyword_identity": identity,
            "trend_cluster_id": cluster,
            "started_at": (observed_at - timedelta(hours=2)).isoformat(),
            "ended_at": observed_at.isoformat(),
            "active": True,
            "search_volume": 10_000 + index + revision * 100,
            "search_volume_display": "10K+",
            "search_volume_semantics": "PROVIDER_REPORTED_APPROXIMATE_BUCKET",
            "increase_percentage": 100.0 + revision,
            "categories": ["synthetic-fixture"],
            "related_queries": None,
        })
    page = TrendingNowPage(
        records=tuple(raw_rows),
        provider_record_count=count,
        rejected_record_count=0,
        provider_status="SUCCESS",
        provider_created_at=observed_at,
        provider_data_at=observed_at,
        http_status=200,
        delivery_state="CACHED",
    )
    records = normalize_trending_candidates(
        page, fetched_at=observed_at, data_mode="LIVE"
    )
    # Keep the accepted Google Trends record contract while making the fixture
    # origin explicit in every row; no provider-reported value is claimed.
    for record in records:
        record.update(
            provider_id="serpapi_google_trends_trending_now",
            transport_id="serpapi",
            discovery_mode="SERPAPI_TRENDING_NOW",
            source_url=TRENDING_NOW_SOURCE_URL,
            quality_flags=["DB06_SYNTHETIC_PROVIDER_FIXTURE"],
        )
        for cluster in record.get("trend_clusters", []):
            cluster["source_url"] = TRENDING_NOW_SOURCE_URL
    return records


def seed_cache(schema: str, count: int, tag: str, revision: int = 0) -> dict:
    if _runtime_environment is None:
        raise RuntimeError("database configuration is unavailable")
    now = datetime.now(timezone.utc)
    records = _fixture_records(count, tag, revision, now)
    # Match the canonical record shape emitted by GoogleTrendsDiscoveryHandler
    # before its result enters SharedCache, including the no-verification value.
    for record in records:
        record["serp_verification"] = {"status": "NOT_CONFIGURED", "results": []}
    grant = CacheGrant(
        GOOGLE_SOURCE_ID,
        GOOGLE_POLICY_VERSION,
        now + timedelta(days=7),
        max_fresh_seconds=60 * 60,
        max_stale_seconds=7 * 24 * 60 * 60,
        max_retention_seconds=7 * 24 * 60 * 60,
        allowed=True,
    )
    scope = trending_now_cache_scope(DEFAULT_QUERY_PLAN.fingerprint, grant)
    payload = {
        "status": "SUCCESS",
        "data_mode": "LIVE",
        "record_count": len(records),
        "records": records,
        "coverage": {
            "discovery_mode": "SYNTHETIC_PROVIDER_FIXTURE",
            "provider_id": "serpapi_google_trends_trending_now",
            "transport_id": "serpapi",
            "provider_record_count": len(records),
            "accepted_record_count": len(records),
            "rejected_record_count": 0,
            "provider_request_count": 0,
            "attempted_calls": 0,
            "trending_candidate_count": len(records),
            "distinct_keyword_count": len({
                record.get("canonical_keyword_identity") for record in records
            }),
            "fetched_at": now.isoformat(),
            "db06_fixture": True,
            "serp_verification_attempted": 0,
            "serp_verified_candidate_count": 0,
            "serp_verification_failed_count": 0,
            "serp_admission_blocked_count": 0,
            "serp_max_queries_per_batch": MAX_VERIFICATION_QUERIES,
            "serp_pages_per_query": 1,
        },
        "quality_flags": ["DB06_SYNTHETIC_PROVIDER_FIXTURE"],
        "provider_request_count": 0,
    }
    store = PostgresAtomicStateStore(_runtime_environment, schema=schema)
    blobs = PostgresSourcePayloads(_runtime_environment, schema=schema)
    cache = SharedCache(
        store, payload_blobs=blobs, max_inline_bytes=500_000, monotonic_read_clock=True
    )
    cache.put(
        scope, grant=grant, origin="SYNTHETIC", payload=payload,
        fetched_at=now, now=now,
    )
    cache_only_payload = {
        **payload,
        "coverage": {**payload["coverage"], "cache_only": True},
    }
    return {"count": len(records), "tag": tag, "revision": revision,
            "fetched_at": now.isoformat(), "scope": scope,
            "source_input_digest": source_input_digest(GOOGLE_SOURCE_ID, cache_only_payload)}


class _NeverReadCredentials:
    def resolve(self, _reference):
        raise AssertionError("DB06 CACHE_ONLY benchmark attempted credential resolution")


def _app_settings(environment: dict[str, str]) -> Settings:
    settings_environment = {
        "TREND_SCOUT_ENVIRONMENT": "local",
        "TREND_SCOUT_RUN_MODE": "live",
        "TREND_SCOUT_READ_ONLY": "false",
        "TREND_SCOUT_PUBLIC_FETCH_ENABLED": "false",
        "TREND_SCOUT_POSTGRES_RUNTIME_ENABLED": "true",
        "TREND_SCOUT_SCHEDULER_ENABLED": "false",
        "TREND_SCOUT_LOCK_PATH": str(Path.cwd() / ".db06-benchmark.lock"),
        "WIKIMEDIA_USER_AGENT": DEFAULT_WIKIMEDIA_USER_AGENT,
        "DATABASE_URL": environment["DATABASE_URL"],
        "DATABASE_URL_UNPOOLED": environment["DATABASE_URL_UNPOOLED"],
    }
    return Settings.from_environment(settings_environment)


def _app_factory(schema: str):
    if _runtime_environment is None:
        raise RuntimeError("database configuration is unavailable")
    from app.main import create_normal_google_live_app

    @asynccontextmanager
    async def composition():
        async with postgres_live_composition(
            runtime_scope="LOCAL",
            user_agent=DEFAULT_WIKIMEDIA_USER_AGENT,
            google_settings=GoogleLiveSettings(),
            credential_provider=_NeverReadCredentials(),
            postgres_environment=_runtime_environment,
            postgres_schema=schema,
        ) as facade:
            yield facade

    return create_normal_google_live_app(
        enabled=True,
        composition_factory=composition,
        settings=_app_settings(_runtime_environment),
        postgres_environment=_runtime_environment,
        postgres_schema=schema,
    )


def _ready_data(schema: str, fetch_id: str) -> dict:
    if _runtime_environment is None:
        raise RuntimeError("database configuration is unavailable")
    namespace = sql.Identifier(schema)
    with postgres_connection(_runtime_environment) as connection:
        row = connection.execute(sql.SQL("""SELECT d.status,d.processing_status,
            d.materialization_status,d.algorithm_processing_status,
            d.ai_classification_status,d.source_ingestion_status,
            d.provider_record_count,d.received_record_count,d.retained_keyword_count,
            (SELECT count(*) FROM {}.dataset_evidence de WHERE de.dataset_id=d.dataset_id),
            (SELECT count(DISTINCT ek.keyword_id)
             FROM {}.dataset_evidence de JOIN {}.evidence_keywords ek
               ON ek.evidence_id=de.evidence_id WHERE de.dataset_id=d.dataset_id)
            FROM {}.dataset_snapshots d WHERE d.dataset_id=%s""").format(
                namespace, namespace, namespace, namespace
            ), (fetch_id,)).fetchone()
    if row is None:
        raise RuntimeError("completed Fetch did not publish a readable dataset snapshot")
    return {
        "dataset_status": row[0],
        "processing_status": row[1],
        "materialization_status": row[2],
        "algorithm_processing_status": row[3],
        "ai_classification_status": row[4],
        "source_ingestion_status": row[5],
        "provider_record_count": row[6],
        "received_record_count": row[7],
        "persisted_keyword_count": row[8],
        "source_evidence_count": row[9],
        "distinct_evidence_keyword_count": row[10],
    }


def _assert_result(measurement_result: dict, expected_count: int,
                   fixture_input_digest: str | None = None) -> None:
    if measurement_result["http_status"] != 202:
        raise RuntimeError("Fetch submission did not return HTTP 202")
    if measurement_result["terminal_status"] not in {"COMPLETED", "PARTIAL"}:
        raise RuntimeError("Fetch did not reach a terminal success state")
    if measurement_result["terminal"] is not True:
        raise RuntimeError("terminal Fetch status was not available")
    if measurement_result["source_record_count"] != expected_count:
        raise RuntimeError("CACHE_ONLY source record count did not match the fixture")
    ready = measurement_result["data_ready"]
    for key in ("received_record_count", "persisted_keyword_count",
                "source_evidence_count", "distinct_evidence_keyword_count"):
        if ready[key] != expected_count:
            raise RuntimeError(f"persisted record integrity failed at {key}")
    if measurement_result["source_provider_requests"] != 0:
        raise RuntimeError("CACHE_ONLY fixture unexpectedly reported provider requests")
    actual_input_digest = measurement_result.get("source_input_digest")
    if not isinstance(actual_input_digest, str) or re.fullmatch(r"[a-f0-9]{64}", actual_input_digest) is None:
        raise RuntimeError("completed Fetch source input digest was invalid")
    if (measurement_result["label"].startswith("case2-warm-changed-")
            and measurement_result["collection_input_unchanged"] is True):
        raise RuntimeError("changed CACHE_ONLY input was incorrectly treated as unchanged")
    if (measurement_result["label"].startswith("case3-warm-unchanged-")
            or measurement_result["label"].startswith("warm-unchanged-")):
        if (measurement_result["collection_input_unchanged"] is not True
                or measurement_result["dataset_reused"] is not True
                or measurement_result["dataset_id"] == measurement_result["fetch_id"]):
            raise RuntimeError("unchanged CACHE_ONLY input did not reuse its durable dataset")
        if measurement_result.get("require_metadata_fast_path"):
            if not measurement_result.get("metadata_only_fast_path_hit"):
                raise RuntimeError("unchanged CACHE_ONLY Fetch missed the metadata-only path")
            if measurement_result["source_payload_bytes_read"] != 0:
                raise RuntimeError("metadata-only Fetch read source payload bytes")
            counts = measurement_result["instrumentation"]["counts"]
            if any(counts.get(key, 0) != 0 for key in (
                    "keyword_persistence_input_records", "source_evidence_input_rows",
                    "dataset_evidence_link_input_rows", "keyword_normalization_records")):
                raise RuntimeError("metadata-only Fetch repeated Collection persistence work")
            if measurement_result["instrumentation"]["stages"].get(
                    "algorithm_processing", {}).get("calls", 0):
                raise RuntimeError("metadata-only Fetch reran collection algorithms")
    if measurement_result["provider_network_requests_blocked"] != 0:
        raise RuntimeError("a provider network request was attempted")
    if measurement_result["openrouter_requests_blocked"] != 0:
        raise RuntimeError("an OpenRouter inference request was attempted")


def _run_fetch(client: TestClient, schema: str, expected_count: int, *, label: str,
               warmup: bool = False, fixture_input_digest: str | None = None,
               require_metadata_fast_path: bool = False) -> dict:
    measurement = tracker.start(label)
    started = time.perf_counter()
    key = "db06-" + uuid.uuid4().hex
    with tracker.span("fetch_submit_to_data_ready", announce=not warmup):
        with tracker.span("http_fetch_submission", announce=not warmup):
            response = client.post(
                "/api/v1/fetches",
                json={"sources": [GOOGLE_SOURCE_ID], "mode": "CACHE_ONLY"},
                headers={"Idempotency-Key": key},
            )
        if response.status_code != 202:
            raise RuntimeError("Fetch endpoint rejected the CACHE_ONLY benchmark request")
        post_payload = response.json()
        fetch_id = post_payload.get("fetch_id")
        if not isinstance(fetch_id, str) or not fetch_id:
            raise RuntimeError("Fetch endpoint did not return a job identifier")

        status_payload = None
        terminal_started = time.perf_counter()
        poll_count = 0
        with tracker.span("terminal_status_wait", announce=not warmup):
            while time.perf_counter() - terminal_started < 60:
                status_response = client.get(f"/api/v1/fetches/{fetch_id}")
                if status_response.status_code != 200:
                    raise RuntimeError("Fetch status endpoint was unavailable")
                status_payload = status_response.json()
                poll_count += 1
                if status_payload.get("terminal") is True:
                    break
                time.sleep(0.05)
            if not isinstance(status_payload, dict) or status_payload.get("terminal") is not True:
                raise TimeoutError("Fetch did not reach a terminal state within 60 seconds")
        post_to_terminal_ms = round((time.perf_counter() - terminal_started) * 1000, 2)

        with tracker.span("terminal_dataset_readiness", announce=not warmup):
            dataset_response = client.get("/api/v1/datasets/latest")
            if dataset_response.status_code != 200:
                raise RuntimeError("dataset latest API did not expose the completed data")
            dataset_id = status_payload.get("dataset_id") or fetch_id
            if not isinstance(dataset_id, str) or not dataset_id:
                raise RuntimeError("terminal Fetch status did not identify its dataset")
            data_ready = _ready_data(schema, dataset_id)
        end_to_end_ms = round((time.perf_counter() - started) * 1000, 2)

    source_rows = status_payload.get("sources")
    google_result = next((item for item in source_rows or []
                          if isinstance(item, dict)
                          and item.get("source_id") == GOOGLE_SOURCE_ID), None)
    if not isinstance(google_result, dict):
        raise RuntimeError("terminal status did not include the selected source")
    source_count = google_result.get("record_count")
    provider_requests = google_result.get("provider_request_count")
    if type(provider_requests) is not int:
        provider_requests = (google_result.get("coverage") or {}).get("provider_request_count")
    result = {
        "label": label,
        "fetch_id": fetch_id,
        "http_status": response.status_code,
        "terminal_status": status_payload.get("status"),
        "terminal": status_payload.get("terminal"),
        "collection_input_unchanged": status_payload.get("collection_input_unchanged"),
        "dataset_id": dataset_id,
        "dataset_reused": status_payload.get("dataset_reused") is True,
        "source_record_count": source_count,
        "source_provider_requests": provider_requests,
        "status_poll_count": poll_count,
        "post_to_terminal_ms": post_to_terminal_ms,
        "end_to_end_ms": end_to_end_ms,
        "data_ready": data_ready,
        "provider_network_requests_blocked": measurement.network_block_count,
        "openrouter_requests_blocked": measurement.openrouter_count,
        "fixture_input_digest": fixture_input_digest,
        "require_metadata_fast_path": require_metadata_fast_path,
    }
    tracker.stop()
    if not warmup:
        result["instrumentation"] = tracker.snapshot(measurement)
    result["source_payload_bytes_read"] = (
        result.get("instrumentation", {}).get("counts", {}).get("payload_blob_read_bytes", 0)
    )
    result["metadata_only_fast_path_hit"] = (
        result.get("instrumentation", {}).get("counts", {}).get(
            "metadata_only_reuse_hits", 0
        ) == 1
    )
    result["source_input_digest"] = _read_source_input_digest(schema, fetch_id)
    if not warmup:
        emit({"event": "fetch_measurement", **result})
    _assert_result(result, expected_count, fixture_input_digest)
    return result


def _read_source_input_digest(schema: str, fetch_id: str) -> str:
    if _runtime_environment is None:
        raise RuntimeError("database configuration is unavailable")
    state = PostgresAtomicStateStore(
        _runtime_environment, schema=schema
    ).read_many([GOOGLE_STATE_KEY]).get(GOOGLE_STATE_KEY)
    job = state.get("jobs", {}).get(fetch_id) if isinstance(state, dict) else None
    checkpoints = job.get("source_checkpoints") if isinstance(job, dict) else None
    checkpoint = checkpoints.get(GOOGLE_SOURCE_ID) if isinstance(checkpoints, dict) else None
    snapshot = checkpoint.get("snapshot") if isinstance(checkpoint, dict) else None
    digest = snapshot.get("source_input_digest") if isinstance(snapshot, dict) else None
    if not isinstance(digest, str) or re.fullmatch(r"[a-f0-9]{64}", digest) is None:
        raise RuntimeError("completed Fetch source input digest was unavailable")
    return digest


def _instrumented_app(schema: str, *, label: str, include_startup: bool):
    if include_startup:
        with tracker.span("application_startup", announce=True):
            application = _app_factory(schema)
            client = TestClient(
                application,
                base_url="http://127.0.0.1:8000",
                client=("127.0.0.1", 51234),
            )
            client.__enter__()
        emit({"event": "application_startup_complete", "measurement": label})
        return application, client
    application = _app_factory(schema)
    client = TestClient(
        application,
        base_url="http://127.0.0.1:8000",
        client=("127.0.0.1", 51234),
    )
    client.__enter__()
    return application, client


def _profile_mode(record_counts=(10, 100, 500)) -> dict:
    schema, setup_seconds = create_isolated_schema()
    emit({"event": "profile_schema_ready", "schema": schema,
          "migration_and_readiness_setup_ms": round(setup_seconds * 1000, 2)})
    tracker.start("profile_startup")
    app, client = _instrumented_app(schema, label="profile_startup", include_startup=True)
    startup_measurement = tracker.stop()
    heartbeat = Heartbeat()
    heartbeat.start()
    rows = []
    try:
        for count in record_counts:
            seed_cache(schema, count, tag=f"profile-{count}", revision=0)
            result = _run_fetch(client, schema, count, label=f"profile-{count}")
            rows.append(result)
        summary = {
            "mode": "profile",
            "profile_record_counts": list(record_counts),
            "schema": schema,
            "baseline_commit": "6e72b698e936038b7c6547270f403d178a595c18",
            "migration_and_fixture_setup_excluded": True,
            "application_startup": tracker.snapshot(startup_measurement),
            "scaling_samples": [
                {
                    "label": row["label"],
                    "source_record_count": row["source_record_count"],
                    "end_to_end_ms": row["end_to_end_ms"],
                    "terminal_status": row["terminal_status"],
                    "provider_network_requests_blocked": row[
                        "provider_network_requests_blocked"
                    ],
                    "openrouter_requests_blocked": row["openrouter_requests_blocked"],
                    "instrumentation": {
                        "connections": row["instrumentation"]["connections"],
                        "sql_executions": row["instrumentation"]["sql"]["executions"],
                        "counts": row["instrumentation"]["counts"],
                        "stages": row["instrumentation"]["stages"],
                    },
                }
                for row in rows
            ],
        }
        emit({"event": "profile_complete", **summary})
        return summary
    finally:
        tracker.stop()
        heartbeat.stop()
        client.__exit__(None, None, None)
        drop_owned_schema(schema)
        emit({"event": "profile_schema_cleanup", "schema": schema,
              "cleaned": schema not in _owned_schemas})


def _cold_case(index: int, count: int = 1500) -> dict:
    schema, setup_seconds = create_isolated_schema()
    seed_cache(schema, count, tag=f"cold-{index}", revision=0)
    startup_started = time.perf_counter()
    startup_measurement = tracker.start(f"case1-cold-{index}-startup")
    app = client = None
    heartbeat = Heartbeat()
    heartbeat.start()
    try:
        app, client = _instrumented_app(
            schema, label=f"case1-cold-{index}", include_startup=True
        )
        startup_ms = round((time.perf_counter() - startup_started) * 1000, 2)
        startup_snapshot = tracker.snapshot(startup_measurement)
        tracker.stop()
        result = _run_fetch(
            client, schema, count, label=f"case1-cold-{index}"
        )
        result["migration_and_fixture_setup_ms"] = round(setup_seconds * 1000, 2)
        result["cold_app_startup_included"] = True
        result["cold_app_startup_ms"] = startup_ms
        result["cold_fetch_to_ready_ms"] = result["end_to_end_ms"]
        result["end_to_end_ms"] = round(startup_ms + result["cold_fetch_to_ready_ms"], 2)
        result["instrumentation"]["application_startup"] = startup_snapshot
        return result
    finally:
        tracker.stop()
        heartbeat.stop()
        if client is not None:
            client.__exit__(None, None, None)
        drop_owned_schema(schema)
        emit({"event": "cold_schema_cleanup", "schema": schema,
              "cleaned": schema not in _owned_schemas})


def _acceptance_mode(*, skip_cold: bool = False, skip_changed: bool = False) -> dict:
    cold = [] if skip_cold else [_cold_case(index) for index in range(1, 4)]
    schema, setup_seconds = create_isolated_schema()
    fixture = seed_cache(schema, 1500, tag="warm-changed", revision=5 if skip_changed else 0)
    tracker.start("warm_application_startup")
    app, client = _instrumented_app(
        schema, label="warm_application_startup", include_startup=True
    )
    startup_measurement = tracker.stop()
    heartbeat = Heartbeat()
    heartbeat.start()
    changed = []
    unchanged = []
    membership = None
    try:
        if not skip_changed:
            previous_fixture_digest = fixture["source_input_digest"]
            previous_actual_digest = None
            for revision in range(1, 6):
                fixture = seed_cache(schema, 1500, tag="warm-changed", revision=revision)
                if fixture["source_input_digest"] == previous_fixture_digest:
                    raise RuntimeError(
                        "changed CACHE_ONLY fixture reused its seeded input digest"
                    )
                result = _run_fetch(
                    client, schema, 1500,
                    label=f"case2-warm-changed-{revision}",
                    fixture_input_digest=fixture["source_input_digest"],
                )
                if result["source_input_digest"] == previous_actual_digest:
                    raise RuntimeError(
                        "changed CACHE_ONLY Fetch reused its prior source input digest"
                    )
                previous_fixture_digest = fixture["source_input_digest"]
                previous_actual_digest = result["source_input_digest"]
                changed.append(result)
                if revision == 1:
                    membership = _preserve_user_membership(schema, result["fetch_id"])
        # Materialize one stable unchanged input outside the five timed samples.
        prime = _run_fetch(
            client, schema, 1500, label="case3-unchanged-prime", warmup=True,
            fixture_input_digest=fixture["source_input_digest"],
        )
        if skip_changed:
            membership = _preserve_user_membership(schema, prime["fetch_id"])
        for index in range(1, 6):
            result = _run_fetch(
                client, schema, 1500, label=f"case3-warm-unchanged-{index}",
                fixture_input_digest=fixture["source_input_digest"],
            )
            if result["source_input_digest"] != prime["source_input_digest"]:
                raise RuntimeError("unchanged CACHE_ONLY fixture changed its input digest")
            unchanged.append(result)
        membership_after = _verify_user_membership(schema, membership)
        summary = {
            "mode": "acceptance",
            "schema": schema,
            "baseline_commit": "6e72b698e936038b7c6547270f403d178a595c18",
            "setup_ms_excluded": round(setup_seconds * 1000, 2),
            "cold_samples_skipped_for_resume": skip_cold,
            "changed_samples_skipped_for_resume": skip_changed,
            "application_startup": tracker.snapshot(startup_measurement),
            "case1_cold": cold,
            "case2_warm_changed": changed,
            "case3_warm_unchanged": unchanged,
            "user_membership": {"before": membership, "after": membership_after},
            "provider_network_requests_blocked": sum(
                row["instrumentation"]["provider_network_requests_blocked"]
                for row in [*cold, *changed, *unchanged]
            ),
            "openrouter_requests_blocked": sum(
                row["instrumentation"]["openrouter_requests_blocked"]
                for row in [*cold, *changed, *unchanged]
            ),
            "warm_latency_ms": {
                "changed_min_median_max": _stats([row["end_to_end_ms"] for row in changed]),
                "unchanged_min_median_max": _stats([row["end_to_end_ms"] for row in unchanged]),
            },
            "warm_all_under_5000ms": all(
                row["end_to_end_ms"] < 5000 for row in [*changed, *unchanged]
            ),
        }
        emit({"event": "acceptance_complete", **summary})
        return summary
    finally:
        tracker.stop()
        heartbeat.stop()
        client.__exit__(None, None, None)
        drop_owned_schema(schema)
        emit({"event": "warm_schema_cleanup", "schema": schema,
              "cleaned": schema not in _owned_schemas})


def _stats(values: list[float]) -> dict:
    if not values:
        return {"samples": 0, "min_ms": None, "median_ms": None, "max_ms": None}
    return {
        "samples": len(values),
        "min_ms": round(min(values), 2),
        "median_ms": round(statistics.median(values), 2),
        "max_ms": round(max(values), 2),
    }


def _warm_unchanged_case(count: int, sample_count: int, *, require_fast_path: bool) -> dict:
    schema, setup_seconds = create_isolated_schema()
    fixture = seed_cache(schema, count, tag=f"warm-unchanged-{count}", revision=0)
    tracker.start(f"warm_{count}_application_startup")
    app, client = _instrumented_app(
        schema, label=f"warm_{count}_application_startup", include_startup=True
    )
    startup_measurement = tracker.stop()
    heartbeat = Heartbeat()
    heartbeat.start()
    samples = []
    membership = None
    try:
        prime = _run_fetch(
            client, schema, count, label=f"warm-prime-{count}", warmup=True,
            fixture_input_digest=fixture["source_input_digest"],
        )
        membership = _preserve_user_membership(schema, prime["fetch_id"])
        preserved_status = {
            key: prime["data_ready"][key] for key in (
                "materialization_status", "algorithm_processing_status",
                "ai_classification_status", "source_ingestion_status",
            )
        }
        for index in range(1, sample_count + 1):
            result = _run_fetch(
                client, schema, count,
                label=f"warm-unchanged-{count}-{index}",
                fixture_input_digest=fixture["source_input_digest"],
                require_metadata_fast_path=require_fast_path,
            )
            if result["source_input_digest"] != prime["source_input_digest"]:
                raise RuntimeError("warm CACHE_ONLY input digest changed after priming")
            if result["dataset_id"] != prime["dataset_id"]:
                raise RuntimeError("warm CACHE_ONLY Fetch did not reuse the primed dataset ID")
            if {key: result["data_ready"][key] for key in preserved_status} != preserved_status:
                raise RuntimeError("warm CACHE_ONLY Fetch changed independent processing status")
            samples.append(result)
        membership_after = _verify_user_membership(schema, membership)
        return {
            "record_count": count,
            "sample_count": sample_count,
            "dataset_id": prime["dataset_id"],
            "setup_ms_excluded": round(setup_seconds * 1000, 2),
            "application_startup": tracker.snapshot(startup_measurement),
            "warm_latency_ms": _stats([item["end_to_end_ms"] for item in samples]),
            "source_payload_bytes_read": [
                item["source_payload_bytes_read"] for item in samples
            ],
            "sql_dispatches": [
                item["instrumentation"]["sql"]["synchronous_dispatches"]
                for item in samples
            ],
            "metadata_only_fast_path_hits": sum(
                item["metadata_only_fast_path_hit"] for item in samples
            ),
            "provider_network_requests_blocked": sum(
                item["provider_network_requests_blocked"] for item in samples
            ),
            "openrouter_requests_blocked": sum(
                item["openrouter_requests_blocked"] for item in samples
            ),
            "processing_status_preserved": preserved_status,
            "user_membership": {"before": membership, "after": membership_after},
            "samples": samples,
        }
    finally:
        tracker.stop()
        heartbeat.stop()
        client.__exit__(None, None, None)
        drop_owned_schema(schema)
        emit({"event": "warm_unchanged_schema_cleanup", "schema": schema,
              "cleaned": schema not in _owned_schemas})


def _warm_unchanged_mode(*, require_fast_path: bool, warm_case: str,
                         large_samples: int) -> dict:
    cases = []
    if warm_case in {"all", "smoke"}:
        cases.append(_warm_unchanged_case(100, 1, require_fast_path=require_fast_path))
    if warm_case in {"all", "large"}:
        cases.append(_warm_unchanged_case(
            1500, large_samples, require_fast_path=require_fast_path
        ))
    summary = {
        "mode": "warm-unchanged",
        "baseline_commit": "6e72b698e936038b7c6547270f403d178a595c18",
        "warm_case": warm_case,
        "large_samples_requested": large_samples if warm_case in {"all", "large"} else 0,
        "require_fast_path": require_fast_path,
        "cases": cases,
        "warm_1500_latency_min_median_max_ms": next(
            (case["warm_latency_ms"] for case in cases if case["record_count"] == 1500),
            None,
        ),
        "provider_network_requests_blocked": sum(
            case["provider_network_requests_blocked"] for case in cases
        ),
        "openrouter_requests_blocked": sum(
            case["openrouter_requests_blocked"] for case in cases
        ),
    }
    emit({"event": "warm_unchanged_complete", **summary})
    return summary


def _preserve_user_membership(schema: str, fetch_id: str) -> dict:
    if _runtime_environment is None:
        raise RuntimeError("database configuration is unavailable")
    store = PostgresCollectionStore(
        _runtime_environment, schema=schema, data_mode_scope="LIVE"
    )
    with postgres_connection(_runtime_environment) as connection:
        row = connection.execute(sql.SQL("""SELECT k.id,k.keyword,k.normalized,k.source,
            k.project,k.observed_at,k.data_mode,k.evidence,k.context FROM {}.keywords k
            ORDER BY k.keyword LIMIT 1""").format(
                sql.Identifier(schema)
            )).fetchone()
    if row is None:
        raise RuntimeError("could not locate a fixture keyword for membership preservation")
    keyword_id = row[0]
    collection = store.create({
        "name": "DB06 user-owned fixture collection",
        "description": "Isolated DB06 membership preservation fixture",
        "examples": [], "exclusions": [], "auto_match": False,
    }, created_by="USER", reason="DB06 isolated acceptance fixture")
    collection_id = collection if isinstance(collection, str) else None
    if not isinstance(collection_id, str):
        raise RuntimeError("could not create the isolated user collection fixture")
    record = dict(zip(("id", "keyword", "normalized", "source", "project",
                       "observed_at", "data_mode", "evidence", "context"), row, strict=True))
    store.feedback_action(collection_id, record, "ADD", pinned=True)
    return {"collection_id": collection_id, "keyword_id": keyword_id,
            "seeded_after_fetch_id": fetch_id}


def _verify_user_membership(schema: str, membership: dict | None) -> dict:
    if not isinstance(membership, dict):
        raise RuntimeError("user membership fixture was not created")
    with postgres_connection(_runtime_environment) as connection:
        row = connection.execute(sql.SQL("""SELECT origin,pinned FROM {}.memberships
            WHERE collection_id=%s AND keyword_id=%s""").format(sql.Identifier(schema)),
            (membership["collection_id"], membership["keyword_id"])).fetchone()
    if row is None or row[0] != "USER" or row[1] not in (True, 1):
        raise RuntimeError("DB06 Fetch did not preserve the user-owned membership")
    return {"preserved": True, "origin": row[0], "pinned": bool(row[1])}


def _verify_test_url_targets_runtime_database(environment: dict[str, str], test_url: str) -> None:
    # The app's pooled and direct URLs must already identify one database. When
    # a dedicated test URL is configured, also verify that it targets that DB.
    from app.storage.postgres_config import verify_same_database

    try:
        verify_same_database(environment)
        with _raw_connect(environment["DATABASE_URL_UNPOOLED"]) as reference:
            reference_identity = reference.execute(
                "SELECT system_identifier, current_database() FROM pg_control_system()"
            ).fetchone()
        with _raw_connect(test_url) as test_connection:
            test_identity = test_connection.execute(
                "SELECT system_identifier, current_database() FROM pg_control_system()"
            ).fetchone()
    except Exception:
        raise RuntimeError("PostgreSQL runtime/test URL identity validation failed") from None
    if reference_identity != test_identity:
        raise RuntimeError("test URL does not identify the configured PostgreSQL database")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode", choices=("profile", "acceptance", "warm-unchanged"), default="profile"
    )
    parser.add_argument(
        "--require-fast-path", action="store_true",
        help="fail warm-unchanged runs unless each sample used metadata-only reuse",
    )
    parser.add_argument(
        "--warm-case", choices=("all", "smoke", "large"), default="all",
        help="select the 100-record smoke, three 1500-record samples, or both",
    )
    parser.add_argument(
        "--large-samples", type=int, choices=(1, 2, 3), default=3,
        help="number of warm unchanged 1500-record samples (default: 3)",
    )
    parser.add_argument(
        "--profile-count", type=int, choices=(10, 100, 500),
        help="run one existing profile fixture size as a smoke measurement",
    )
    parser.add_argument(
        "--skip-cold", action="store_true",
        help="resume acceptance warm cases after the three cold samples completed",
    )
    parser.add_argument(
        "--skip-changed", action="store_true",
        help="run unchanged warm acceptance after changed samples were measured",
    )
    arguments = parser.parse_args()
    if arguments.skip_cold and arguments.mode != "acceptance":
        parser.error("--skip-cold is only valid with --mode acceptance")
    if arguments.skip_changed and (arguments.mode != "acceptance" or not arguments.skip_cold):
        parser.error("--skip-changed requires --mode acceptance and --skip-cold")
    if arguments.profile_count is not None and arguments.mode != "profile":
        parser.error("--profile-count is only valid with --mode profile")
    if arguments.require_fast_path and arguments.mode != "warm-unchanged":
        parser.error("--require-fast-path is only valid with --mode warm-unchanged")
    if arguments.warm_case != "all" and arguments.mode != "warm-unchanged":
        parser.error("--warm-case is only valid with --mode warm-unchanged")
    if arguments.large_samples != 3 and arguments.mode != "warm-unchanged":
        parser.error("--large-samples is only valid with --mode warm-unchanged")
    if os.environ.get("VERCEL"):
        raise RuntimeError("DB06 local benchmark refuses a Vercel process")
    explicit_test_url = bool(os.environ.get("TRENDSCOUT_TEST_POSTGRES_URL"))
    environment, test_url = _test_database_configuration()
    emit({"event": "test_database_configuration_loaded", "pooled_url_present": bool(
        environment.get("DATABASE_URL")
    ), "direct_url_present": bool(environment.get("DATABASE_URL_UNPOOLED")),
          "test_url_source": "process" if explicit_test_url else "local_env_fallback"})
    _verify_test_url_targets_runtime_database(environment, test_url)
    emit({"event": "test_database_identity_verified"})
    install_instrumentation()
    # Keep the in-process OpenRouter client deliberately credentialless. The
    # value is never read from or written to a dotenv file.
    os.environ["OPENROUTER_API_KEY"] = ""
    emit({"event": "benchmark_started", "mode": arguments.mode,
          "baseline_commit": "6e72b698e936038b7c6547270f403d178a595c18",
          "database_identity_validated": True,
          "schema_prefix": "ts_test_db06_",
          "provider_network": "blocked",
          "openrouter": "blocked"})
    if arguments.mode == "profile":
        _profile_mode((arguments.profile_count,) if arguments.profile_count else (10, 100, 500))
    elif arguments.mode == "acceptance":
        _acceptance_mode(skip_cold=arguments.skip_cold,
                         skip_changed=arguments.skip_changed)
    else:
        _warm_unchanged_mode(
            require_fast_path=arguments.require_fast_path, warm_case=arguments.warm_case,
            large_samples=arguments.large_samples,
        )
    cleanup_owned_schemas()
    return 0


def _handle_termination(_signum, _frame):
    raise KeyboardInterrupt("DB06 benchmark interrupted; cleaning only owned schemas")


if __name__ == "__main__":
    for _signal in (signal.SIGINT, signal.SIGTERM):
        signal.signal(_signal, _handle_termination)
    try:
        raise SystemExit(main())
    except SystemExit:
        # The runner intentionally exits through SystemExit(main()). Let a
        # successful return reach the process status without classifying it as
        # a benchmark failure; its mode-level finally blocks own normal cleanup.
        cleanup_owned_schemas()
        raise
    except KeyboardInterrupt:
        tracker.stop()
        cleanup_owned_schemas()
        emit({"event": "benchmark_interrupted", "cleanup_complete": not _owned_schemas})
        raise SystemExit(130)
    except BaseException as error:
        tracker.stop()
        cleanup_owned_schemas()
        frames = traceback.extract_tb(error.__traceback__)
        location = None
        if frames:
            frame = frames[-1]
            location = f"{Path(frame.filename).name}:{frame.lineno}:{frame.name}"
        emit({"event": "benchmark_failed", "error_type": type(error).__name__,
              "missing_module": getattr(error, "name", None),
              "location": location, "cleanup_complete": not _owned_schemas})
        raise SystemExit(1)
