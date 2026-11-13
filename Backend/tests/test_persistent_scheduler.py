import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from app.demo_local import DemoFixtureAdapter
from app.main import create_app
from app.scheduling.persistent import PersistentScheduleStore, ScheduleWorker
from app.settings import Settings
from app.storage.fake import FakeStorage

UTC = timezone.utc


class PersistentSchedulerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = PersistentScheduleStore(Path(self.directory.name) / "schedules.sqlite")
        self.now = datetime(2026, 11, 10, 11, tzinfo=UTC)

    def tearDown(self):
        self.directory.cleanup()

    def create(self, *, frequency="DAILY", hour=10):
        return self.store.create(name="Nightly fixture refresh", frequency=frequency,
            timezone_name="UTC", hour=hour, now=self.now)

    def test_schedule_persists_disabled_and_enable_calculates_next_local_time(self):
        item = self.create()
        self.assertFalse(item["enabled"])
        self.assertEqual(item["next_run_at"], "2026-11-11T10:00:00+00:00")
        self.store = PersistentScheduleStore(Path(self.directory.name) / "schedules.sqlite")
        enabled = self.store.set_enabled(item["id"], True, now=self.now)
        self.assertTrue(enabled["enabled"])
        self.assertEqual(enabled["timezone"], "UTC")

    def test_weekly_frequency_skips_missed_runs_and_persists_status(self):
        item = self.create(frequency="WEEKLY")
        self.store.set_enabled(item["id"], True, now=self.now)
        # Two full weekly occurrences were missed; only the latest due one runs.
        late = datetime(2026, 11, 30, 12, tzinfo=UTC)
        due = self.store.claim_due(now=late)
        self.assertIsNotNone(due)
        self.assertEqual(due["scheduled_at"], "2026-11-16T10:00:00+00:00")
        self.store.finish(due["occurrence_id"], status="COMPLETED", now=late)
        saved = self.store.get(item["id"])
        self.assertEqual(saved["last_status"], "COMPLETED")
        self.assertEqual(saved["missed_runs"], 2)
        self.assertIsNone(self.store.claim_due(now=late))

    def test_expired_lease_reuses_idempotency_key_after_restart(self):
        item = self.create()
        self.store.set_enabled(item["id"], True, now=self.now)
        due_at = datetime.fromisoformat(item["next_run_at"])
        first = self.store.claim_due(now=due_at, lease=timedelta(seconds=5))
        self.assertIsNotNone(first)
        restarted = PersistentScheduleStore(Path(self.directory.name) / "schedules.sqlite")
        recovered = restarted.claim_due(now=due_at + timedelta(seconds=6))
        self.assertTrue(recovered["recovered"])
        self.assertEqual(first["occurrence_id"], recovered["occurrence_id"])
        restarted.finish(recovered["occurrence_id"], status="PARTIAL", now=due_at + timedelta(seconds=8))
        restarted.finish(recovered["occurrence_id"], status="COMPLETED", now=due_at + timedelta(seconds=9))
        self.assertEqual(restarted.get(item["id"])["last_status"], "PARTIAL")

    def test_worker_uses_mock_callback_and_stops_safely(self):
        async def scenario():
            item = self.create()
            self.store.set_enabled(item["id"], True, now=self.now)
            due_at = datetime.fromisoformat(item["next_run_at"])
            # Move the due time to now in the isolated fixture database.
            with self.store._connect() as db:
                db.execute("UPDATE schedules SET next_run_at=? WHERE id=?", (due_at.isoformat(), item["id"]))
            calls = []

            async def mock_fetch(occurrence):
                calls.append(occurrence["occurrence_id"])
                return "COMPLETED"

            worker = ScheduleWorker(self.store, mock_fetch, poll_seconds=0.1)
            await worker.start()
            for _ in range(30):
                if self.store.get(item["id"])["last_status"]:
                    break
                await asyncio.sleep(0.02)
            await worker.stop()
            self.assertEqual(len(calls), 1)
            self.assertEqual(self.store.get(item["id"])["last_status"], "COMPLETED")
            self.assertIsNone(worker._task)

        asyncio.run(scenario())

    def test_rejects_invalid_timezone_frequency_and_boolean_coercion(self):
        with self.assertRaises(ValueError):
            self.store.create(name="x", frequency="HOURLY", timezone_name="UTC", now=self.now)
        with self.assertRaises(ValueError):
            self.store.create(name="x", frequency="DAILY", timezone_name="Mars/Olympus", now=self.now)
        item = self.create()
        with self.assertRaises(ValueError):
            self.store.set_enabled(item["id"], 1, now=self.now)

    def test_operator_api_persists_schedules_and_rejects_missing_token(self):
        async def scenario():
            token = "test-only-scheduler-operator-token"
            settings = Settings.from_environment({
                "TREND_SCOUT_ENVIRONMENT": "test",
                "TREND_SCOUT_RUN_MODE": "synthetic",
                "TREND_SCOUT_OPERATOR_TOKEN": token,
                "TREND_SCOUT_LOCK_PATH": str(Path(self.directory.name) / "run.lock"),
                "WIKIMEDIA_USER_AGENT": "TrendScout test (mailto:test@example.invalid)",
            })
            app = create_app(settings=settings, read_only=False, scheduler_store=self.store)
            app.state.operator_token = token
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
                denied = await client.post("/api/v1/scheduler", json={
                    "name": "Daily", "frequency": "DAILY", "timezone": "UTC", "hour": 9,
                })
                self.assertEqual(denied.status_code, 401)
                created = await client.post("/api/v1/scheduler", headers={"Authorization": f"Bearer {token}"}, json={
                    "name": "Daily", "frequency": "DAILY", "timezone": "UTC", "hour": 9,
                })
                self.assertEqual(created.status_code, 201)
                schedule_id = created.json()["item"]["id"]
                enabled = await client.patch(f"/api/v1/scheduler/{schedule_id}", headers={"Authorization": f"Bearer {token}"}, json={"enabled": True})
                self.assertTrue(enabled.json()["item"]["enabled"])
                listed = await client.get("/api/v1/scheduler")
                self.assertEqual([item["id"] for item in listed.json()["items"]], [schedule_id])

        asyncio.run(scenario())

    def test_app_worker_lifecycle_is_configured_synthetic_only_and_safe_when_empty(self):
        async def scenario():
            token = "test-only-scheduler-operator-token"
            settings = Settings.from_environment({
                "TREND_SCOUT_ENVIRONMENT": "test",
                "TREND_SCOUT_RUN_MODE": "synthetic",
                "TREND_SCOUT_PUBLIC_FETCH_ENABLED": "true",
                "TREND_SCOUT_SCHEDULER_ENABLED": "true",
                "TREND_SCOUT_OPERATOR_TOKEN": token,
                "TREND_SCOUT_LOCK_PATH": str(Path(self.directory.name) / "run.lock"),
            })
            app = create_app(settings=settings, storage=FakeStorage(),
                adapter_factory=DemoFixtureAdapter, scheduler_store=self.store)
            async with app.router.lifespan_context(app):
                self.assertTrue(app.state.scheduler_worker._task)
                self.assertEqual(self.store.list(), [])
                self.assertEqual(await app.state.run_service.storage.list_runs(), [])
            self.assertIsNone(app.state.scheduler_worker._task)
            self.assertEqual(self.store.list(), [])

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
