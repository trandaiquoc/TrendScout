"""Fenced PostgreSQL scheduler operations, with no provider execution."""

import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from psycopg import sql

from app.scheduling.common import ScheduleBusyError, ScheduleLeaseLost
from app.scheduling.postgres import PostgresScheduleStore
from app.storage.postgres_connection import postgres_connection
from app.storage.postgres_migrations import apply_migrations


def test_concurrent_claim_recovery_and_fencing_in_isolated_schema():
    url = os.environ.get("TRENDSCOUT_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("TRENDSCOUT_TEST_POSTGRES_URL is not configured")
    environment = {"DATABASE_URL": url, "DATABASE_URL_UNPOOLED": url}
    schema = "ts_test_" + uuid.uuid4().hex
    created = False
    with postgres_connection(environment, direct=True) as connection:
        connection.autocommit = True
        assert not connection.execute(
            "SELECT EXISTS (SELECT FROM pg_namespace WHERE nspname=%s)", (schema,)
        ).fetchone()[0]
        try:
            apply_migrations(connection, schema=schema)
            created = True
            store = PostgresScheduleStore(environment, schema=schema)
            now = datetime(2026, 11, 25, tzinfo=timezone.utc)
            schedule = store.create(name="Synthetic", frequency="DAILY", timezone_name="UTC",
                                    hour=1, minute=0, now=now)
            assert schedule["enabled"] == 0
            store.set_enabled(schedule["id"], True, now=now)
            due = now + timedelta(hours=1)
            with ThreadPoolExecutor(max_workers=4) as executor:
                claims = list(executor.map(lambda _index: store.claim_due(now=due, lease=timedelta(seconds=5)),
                                           range(4)))
            winners = [claim for claim in claims if claim is not None]
            assert len(winners) == 1
            first = winners[0]
            with pytest.raises(ScheduleBusyError):
                store.update(schedule["id"], name="Synthetic", frequency="DAILY",
                             timezone_name="UTC", hour=2, minute=0, now=due)
            recovered = store.claim_due(now=due + timedelta(seconds=6), lease=timedelta(seconds=5))
            assert recovered["occurrence_id"] == first["occurrence_id"]
            assert recovered["recovered"] is True and recovered["fence"] > first["fence"]
            with pytest.raises(ScheduleLeaseLost):
                store.finish(first["occurrence_id"], status="COMPLETED", fence=first["fence"],
                             now=due + timedelta(seconds=6))
            store.finish(recovered["occurrence_id"], status="PARTIAL", fence=recovered["fence"],
                         now=due + timedelta(seconds=7))
            assert store.get(schedule["id"])["last_status"] == "PARTIAL"
            assert len(store.list()) == 1
        finally:
            if created:
                connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
