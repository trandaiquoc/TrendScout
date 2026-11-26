"""PostgreSQL schedules with fenced occurrence leases; not wired to runtime."""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from app.scheduling.common import (
    FREQUENCIES,
    ScheduleBusyError,
    ScheduleLeaseLost,
    _iso,
    _next_run,
    _utc_now,
    _valid_zone,
)
from app.storage.postgres_connection import PostgresConnectionError, postgres_connection
from app.storage.postgres_migrations import _SCHEMA_NAME


class ScheduleStorageError(RuntimeError):
    """The schedule database could not complete an operation."""


def _result(row):
    if row is None:
        return None
    return {key: _iso(value) if isinstance(value, datetime) else value for key, value in row.items()}


def _validate(name, frequency, timezone_name, hour, minute):
    name = name.strip() if isinstance(name, str) else ""
    if not name or len(name) > 80:
        raise ValueError("schedule name must contain 1 to 80 characters")
    if (frequency not in FREQUENCIES or type(hour) is not int or not 0 <= hour <= 23
            or type(minute) is not int or not 0 <= minute <= 59):
        raise ValueError("invalid frequency or local time")
    _valid_zone(timezone_name)
    return name


class PostgresScheduleStore:
    def __init__(self, environment, *, schema="trendscout"):
        if not isinstance(schema, str) or not _SCHEMA_NAME.fullmatch(schema):
            raise ValueError("invalid PostgreSQL schema name")
        self._environment, self._schema = dict(environment), schema

    def _table(self, name):
        return sql.SQL("{}.{}").format(sql.Identifier(self._schema), sql.Identifier(name))

    @contextmanager
    def _transaction(self):
        try:
            with postgres_connection(self._environment) as db:
                db.row_factory = dict_row
                with db.transaction():
                    db.execute("SELECT set_config('lock_timeout', '10s', true)")
                    db.execute("SELECT set_config('statement_timeout', '30s', true)")
                    db.execute("SELECT set_config('idle_in_transaction_session_timeout', '30s', true)")
                    yield db
        except (psycopg.Error, PostgresConnectionError):
            raise ScheduleStorageError("schedule database operation failed") from None

    def create(self, *, name, frequency, timezone_name, hour=0, minute=0, now=None):
        now = now or _utc_now()
        _iso(now)
        name = _validate(name, frequency, timezone_name, hour, minute)
        next_run = _next_run(now, frequency, timezone_name, hour, minute)
        with self._transaction() as db:
            row = db.execute(sql.SQL("""INSERT INTO {}(id,name,frequency,timezone,hour,minute,
                next_run_at,created_at,updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""")
                .format(self._table("schedules")),
                (uuid.uuid4().hex, name, frequency, timezone_name, hour, minute, next_run, now, now)).fetchone()
            return _result(row)

    def get(self, schedule_id):
        with self._transaction() as db:
            return _result(db.execute(sql.SQL("SELECT * FROM {} WHERE id=%s")
                                      .format(self._table("schedules")), (schedule_id,)).fetchone())

    def list(self):
        with self._transaction() as db:
            rows = db.execute(sql.SQL("SELECT * FROM {} ORDER BY created_at,id")
                              .format(self._table("schedules"))).fetchall()
            return [_result(row) for row in rows]

    def update(self, schedule_id, *, name, frequency, timezone_name, hour, minute, now=None):
        now = now or _utc_now()
        _iso(now)
        name = _validate(name, frequency, timezone_name, hour, minute)
        next_run = _next_run(now, frequency, timezone_name, hour, minute)
        with self._transaction() as db:
            row = db.execute(sql.SQL("SELECT id FROM {} WHERE id=%s FOR UPDATE")
                             .format(self._table("schedules")), (schedule_id,)).fetchone()
            if row is None:
                return None
            running = db.execute(sql.SQL("""SELECT 1 FROM {} WHERE schedule_id=%s AND status='RUNNING'
                LIMIT 1""").format(self._table("schedule_occurrences")), (schedule_id,)).fetchone()
            if running:
                raise ScheduleBusyError("schedule has a running occurrence")
            row = db.execute(sql.SQL("""UPDATE {} SET name=%s,frequency=%s,timezone=%s,hour=%s,minute=%s,
                next_run_at=%s,updated_at=%s WHERE id=%s RETURNING *""").format(self._table("schedules")),
                (name, frequency, timezone_name, hour, minute, next_run, now, schedule_id)).fetchone()
            return _result(row)

    def set_enabled(self, schedule_id, enabled, *, now=None):
        if type(enabled) is not bool:
            raise ValueError("enabled must be boolean")
        now = now or _utc_now()
        _iso(now)
        with self._transaction() as db:
            row = db.execute(sql.SQL("SELECT * FROM {} WHERE id=%s FOR UPDATE")
                             .format(self._table("schedules")), (schedule_id,)).fetchone()
            if row is None:
                return None
            next_run = row["next_run_at"]
            if enabled and not row["enabled"]:
                next_run = _next_run(now, row["frequency"], row["timezone"], row["hour"], row["minute"])
            row = db.execute(sql.SQL("UPDATE {} SET enabled=%s,next_run_at=%s,updated_at=%s "
                                     "WHERE id=%s RETURNING *").format(self._table("schedules")),
                             (int(enabled), next_run, now, schedule_id)).fetchone()
            return _result(row)

    def claim_due(self, *, now=None, lease=timedelta(minutes=5)):
        now = now or _utc_now()
        _iso(now)
        if not isinstance(lease, timedelta) or lease <= timedelta(0):
            raise ValueError("positive scheduler lease required")
        lease_until = now + lease
        with self._transaction() as db:
            expired = db.execute(sql.SQL("""SELECT o.id AS occurrence_id,o.scheduled_at AS occurrence_time,
                o.fence AS occurrence_fence,s.* FROM {} o JOIN {} s ON s.id=o.schedule_id
                WHERE o.status='RUNNING' AND o.lease_until<=%s ORDER BY o.scheduled_at,o.id
                LIMIT 1 FOR UPDATE OF o,s SKIP LOCKED""")
                .format(self._table("schedule_occurrences"), self._table("schedules")), (now,)).fetchone()
            if expired:
                occurrence_id = expired.pop("occurrence_id")
                scheduled_at = expired.pop("occurrence_time")
                fence = expired.pop("occurrence_fence") + 1
                if expired["enabled"]:
                    db.execute(sql.SQL("UPDATE {} SET lease_until=%s,fence=%s WHERE id=%s")
                               .format(self._table("schedule_occurrences")), (lease_until, fence, occurrence_id))
                    return {"occurrence_id": occurrence_id, "schedule": _result(expired),
                            "scheduled_at": _iso(scheduled_at), "recovered": True, "fence": fence}
                db.execute(sql.SQL("""UPDATE {} SET status='SKIPPED',lease_until=NULL,finished_at=%s,
                    error_code='SCHEDULE_DISABLED' WHERE id=%s""")
                    .format(self._table("schedule_occurrences")), (now, occurrence_id))
            row = db.execute(sql.SQL("""SELECT * FROM {} WHERE enabled=1 AND next_run_at<=%s
                ORDER BY next_run_at,id LIMIT 1 FOR UPDATE SKIP LOCKED""")
                .format(self._table("schedules")), (now,)).fetchone()
            if row is None:
                return None
            scheduled_at = row["next_run_at"]
            occurrence_id = uuid.uuid5(uuid.NAMESPACE_URL, f"schedule:{row['id']}:{_iso(scheduled_at)}").hex
            inserted = db.execute(sql.SQL("""INSERT INTO {}(id,schedule_id,scheduled_at,status,lease_until,
                fence,started_at) VALUES (%s,%s,%s,'RUNNING',%s,1,%s) ON CONFLICT DO NOTHING RETURNING id""")
                .format(self._table("schedule_occurrences")),
                (occurrence_id, row["id"], scheduled_at, lease_until, now)).fetchone()
            if inserted is None:
                raise ScheduleStorageError("schedule occurrence already exists")
            next_run = _next_run(now, row["frequency"], row["timezone"], row["hour"], row["minute"])
            missed = max(0, int((now - scheduled_at).total_seconds() // FREQUENCIES[row["frequency"]].total_seconds()))
            db.execute(sql.SQL("UPDATE {} SET next_run_at=%s,updated_at=%s,missed_runs=missed_runs+%s WHERE id=%s")
                       .format(self._table("schedules")), (next_run, now, missed, row["id"]))
            return {"occurrence_id": occurrence_id, "schedule": _result(row),
                    "scheduled_at": _iso(scheduled_at), "recovered": False, "fence": 1}

    def finish(self, occurrence_id, *, status, fence, now=None, error_code=None):
        if status not in {"COMPLETED", "PARTIAL", "FAILED", "ABORTED"}:
            raise ValueError("invalid schedule run status")
        if type(fence) is not int or fence < 1:
            raise ValueError("valid schedule fence required")
        now = now or _utc_now()
        _iso(now)
        with self._transaction() as db:
            occurrence = db.execute(sql.SQL("SELECT * FROM {} WHERE id=%s FOR UPDATE")
                                    .format(self._table("schedule_occurrences")), (occurrence_id,)).fetchone()
            if occurrence is None:
                raise ValueError("unknown schedule occurrence")
            if occurrence["fence"] != fence:
                raise ScheduleLeaseLost("schedule occurrence lease lost")
            if occurrence["status"] in {"COMPLETED", "PARTIAL", "FAILED", "ABORTED"}:
                return
            if (occurrence["status"] != "RUNNING" or occurrence["lease_until"] is None
                    or occurrence["lease_until"] <= now):
                raise ScheduleLeaseLost("schedule occurrence lease lost")
            db.execute(sql.SQL("""UPDATE {} SET status=%s,lease_until=NULL,finished_at=%s,error_code=%s
                WHERE id=%s""").format(self._table("schedule_occurrences")),
                (status, now, error_code, occurrence_id))
            db.execute(sql.SQL("UPDATE {} SET last_run_at=%s,last_status=%s,updated_at=%s WHERE id=%s")
                       .format(self._table("schedules")), (now, status, now, occurrence["schedule_id"]))
