import asyncio
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

import httpx
from google.cloud.firestore_v1._helpers import encode_dict

from app.adapters.wikimedia import CollectionResult, SOURCE
from app.main import create_app
from app.models.observations import DatasetSnapshot, FetchStatus, PageObservation
from app.orchestration.runs import HostRunLock, RunBusy, RunService
from app.processing.wikimedia_cleaning import clean_snapshot
from app.storage.base import StorageError
from app.storage.fake import FakeStorage

DAY = date(2026, 11, 6)
NOW = datetime(2026, 11, 9, tzinfo=timezone.utc)
TOKEN = "test-only-operator-token-123456789"


def make_row(day, title, rank=1):
    return PageObservation(source=SOURCE, country_scope="US", access="all-access", observation_date=day,
        project="en.wikipedia", article_original=title, article_display=title, canonical_keyword=title,
        views_ceil=100 if day != DAY else 500, provider_rank=rank, fetched_at=NOW,
        source_url="https://en.wikipedia.org/wiki/Example", raw_record={"article": title, "project": "en.wikipedia", "rank": rank, "views_ceil": 100})


def make_snapshot(day, rows, status=FetchStatus.SUCCESS, code=200, error=None):
    return DatasetSnapshot(source=SOURCE, country_scope="US", access="all-access", observation_date=day,
        fetched_at=NOW, fetch_status=status, http_status=code, item_count=len(rows) if status == FetchStatus.SUCCESS else None,
        request_url="https://wikimedia.org/test", request_count=1, rejected_count=0 if status == FetchStatus.SUCCESS else None, error=error)


def fixture():
    days = []
    for offset in range(8):
        day = DAY-timedelta(days=offset)
        rows = [make_row(day, "Paris_Fashion_Week"), make_row(day, "Cat", 2)] if offset < 3 else [make_row(day, "Paris_Fashion_Week")]
        # Cleaning is normally applied to raw adapter titles.
        if offset == 6:
            days.append((make_snapshot(day, [], FetchStatus.DAY_UNAVAILABLE, 404, "HTTP 404"), []))
        elif offset == 7:
            days.append((make_snapshot(day, [], FetchStatus.FETCH_ERROR, 429, "HTTP 429"), []))
        else:
            days.append((make_snapshot(day, rows), rows))
    return CollectionResult(DAY, tuple(days))


class StubAdapter:
    async def collect(self):
        return fixture()


class BlockingAdapter:
    event = None
    async def collect(self):
        await self.event.wait()
        return fixture()


async def wait_run(client, run_id):
    for _ in range(200):
        value = (await client.get(f"/api/runs/{run_id}")).json()
        if value["status"] in ("COMPLETED", "PARTIAL", "FAILED", "ABORTED"):
            return value
        await asyncio.sleep(0.005)
    raise AssertionError("run did not finish")


class ApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = FakeStorage()
        self.path = str(Path(self.tmp.name) / "lock")
        self.app = create_app(storage=self.store, adapter_factory=StubAdapter, lock_path=self.path, operator_token=TOKEN)
        self.lifespan = self.app.router.lifespan_context(self.app)
        await self.lifespan.__aenter__()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test")

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.lifespan.__aexit__(None, None, None)
        self.tmp.cleanup()

    async def test_full_pipeline_search_filter_detail_and_restart(self):
        self.assertEqual((await self.client.get("/api/health")).json(), {"status": "ok"})
        self.assertEqual((await self.client.post("/api/runs")).status_code, 401)
        created = await self.client.post("/api/runs", headers={"Authorization": f"Bearer {TOKEN}"})
        self.assertEqual(created.status_code, 202)
        run_id = created.json()["run_id"]
        run = await wait_run(self.client, run_id)
        self.assertEqual(run["status"], "PARTIAL")
        self.assertEqual(run["keyword_count"], 2)
        self.assertEqual(len(await self.store.list_records(run_id, "snapshots")), 8)
        observations = await self.store.list_records(run_id, "observations")
        self.assertEqual(sum(len(bucket["items"]) for bucket in observations), run["observation_count"])
        # Check all actual fake-run documents can be encoded by the Firestore SDK.
        for kind in ("snapshots", "observations", "keyword_results"):
            for record in await self.store.list_records(run_id, kind):
                encode_dict(record)
        self.assertTrue(any(x["http_status"] == 404 for x in run["coverage"]))
        self.assertTrue(any(x["http_status"] == 429 for x in run["coverage"]))
        for cat, expected in [("all", 2), ("fashion", 1), ("home_goods", 0), ("unclassified", 1)]:
            response = await self.client.get(f"/api/runs/{run_id}/keywords", params={"category": cat})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["total"], expected)
            if not expected:
                self.assertEqual(response.json()["empty_reason"], "NO_MATCHES_IN_ANALYZED_DATA")
        page = (await self.client.get(f"/api/runs/{run_id}/keywords", params={"q": "cat", "limit": 1})).json()
        self.assertEqual(page["total"], 1)
        self.assertIsNone(page["items"][0]["score"])
        first_page = (await self.client.get(f"/api/runs/{run_id}/keywords", params={"limit": 1, "sort": "keyword"})).json()
        second_page = (await self.client.get(f"/api/runs/{run_id}/keywords", params={"limit": 1, "offset": first_page["next_offset"], "sort": "keyword"})).json()
        self.assertNotEqual(first_page["items"][0]["keyword_id"], second_page["items"][0]["keyword_id"])
        keyword_id = page["items"][0]["keyword_id"]
        detail = (await self.client.get(f"/api/runs/{run_id}/keywords/{keyword_id}")).json()
        self.assertEqual(detail["status"], "INSUFFICIENT_DATA")
        self.assertIsNone(detail["score"])
        self.assertEqual(detail["evidence_url"], "https://en.wikipedia.org/wiki/Example")
        await self.lifespan.__aexit__(None, None, None)
        self.lifespan = self.app.router.lifespan_context(self.app)
        await self.lifespan.__aenter__()
        self.assertEqual((await self.client.get(f"/api/runs/{run_id}/keywords/{keyword_id}")).status_code, 200)

    async def test_duplicate_concurrent_and_cooldown(self):
        BlockingAdapter.event = asyncio.Event()
        self.app.state.run_service.adapter_factory = BlockingAdapter
        headers = {"Authorization": f"Bearer {TOKEN}"}
        first = await self.client.post("/api/runs", headers=headers)
        self.assertEqual(first.status_code, 202)
        self.assertEqual((await self.client.post("/api/runs", headers=headers)).status_code, 409)
        BlockingAdapter.event.set()
        await wait_run(self.client, first.json()["run_id"])
        self.assertEqual((await self.client.post("/api/runs", headers=headers)).status_code, 429)

    async def test_recover_interrupted_run(self):
        run_id = "a" * 32
        await self.store.create_run(run_id, {"status": "FETCHING"})
        lock = HostRunLock(self.path, 0)
        lock.acquire(ignore_cooldown=True)
        lock.write({"active_run_id": run_id})
        lock.release()
        await self.app.state.run_service.recover()
        self.assertEqual((await self.store.get_run(run_id))["status"], "ABORTED")

    async def test_independent_lock_instances_exclude_each_other(self):
        first, second = HostRunLock(self.path, 0), HostRunLock(self.path, 0)
        first.acquire(ignore_cooldown=True)
        try:
            with self.assertRaises(RunBusy):
                second.acquire(ignore_cooldown=True)
        finally:
            first.release()
        second.acquire(ignore_cooldown=True)
        second.release()

    async def test_write_failure_is_not_success(self):
        class FailingStorage(FakeStorage):
            async def put_record(self, run_id, kind, record_id, data):
                raise StorageError("test failure")
        self.app.state.run_service.storage = FailingStorage()
        response = await self.client.post("/api/runs", headers={"Authorization": f"Bearer {TOKEN}"})
        run = await wait_run(self.client, response.json()["run_id"])
        self.assertEqual(run["status"], "FAILED")
        self.assertIn("StorageError", run["error"])

    async def test_firestore_failure_after_some_writes(self):
        class FailingStorage(FakeStorage):
            def __init__(self):
                super().__init__()
                self.writes = 0
            async def put_record(self, run_id, kind, record_id, data):
                self.writes += 1
                if self.writes == 2:
                    raise StorageError("test failure")
                await super().put_record(run_id, kind, record_id, data)
        self.app.state.run_service.storage = FailingStorage()
        response = await self.client.post("/api/runs", headers={"Authorization": f"Bearer {TOKEN}"})
        run_id = response.json()["run_id"]
        run = await wait_run(self.client, run_id)
        self.assertEqual(run["status"], "PARTIAL")
        self.assertEqual(run["document_writes"], 1)
        self.assertEqual((await self.client.get(f"/api/runs/{run_id}/keywords")).status_code, 409)

    async def test_fetch_timeout_records_error_and_no_score(self):
        class TimeoutAdapter:
            async def collect(self):
                snapshot = make_snapshot(DAY, [], FetchStatus.FETCH_ERROR, None, "ReadTimeout")
                return CollectionResult(None, ((snapshot, []),))
        self.app.state.run_service.adapter_factory = TimeoutAdapter
        response = await self.client.post("/api/runs", headers={"Authorization": f"Bearer {TOKEN}"})
        run = await wait_run(self.client, response.json()["run_id"])
        self.assertEqual(run["status"], "FAILED")
        self.assertEqual(run["coverage"][0]["error"], "ReadTimeout")


class CleaningRegressionTests(unittest.TestCase):
    def test_localized_main_pages_only_for_matching_project(self):
        ja = replace(make_row(DAY, "メインページ"), project="ja.wikipedia")
        fr = replace(make_row(DAY, "Wikipédia:Accueil_principal"), project="fr.wikipedia")
        content = replace(make_row(DAY, "メインページの歴史"), project="ja.wikipedia")
        different_wiki = replace(make_row(DAY, "メインページ"), project="en.wikipedia")
        rows = [ja, fr, content, different_wiki]
        result = clean_snapshot(make_snapshot(DAY, rows), rows)
        self.assertEqual(result.report.rejection_reasons, {"TECHNICAL_LOCALIZED_MAIN_PAGE": 2})
        self.assertEqual([x.article_original for x in result.accepted], ["メインページの歴史", "メインページ"])
