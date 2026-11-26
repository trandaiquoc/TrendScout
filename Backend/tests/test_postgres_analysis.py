"""Existing analysis-storage contract against an isolated PostgreSQL schema."""

import os
import unittest
import uuid
from datetime import timezone

from psycopg import sql
from test_repository_contract import ContractMixin

from app.storage.analysis_document_codec import AnalysisTimestamp
from app.storage.postgres_analysis import PostgresAnalysisStorage
from app.storage.postgres_connection import postgres_connection
from app.storage.postgres_migrations import apply_migrations


class PostgresAnalysisContract(ContractMixin, unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        url = os.environ.get("TRENDSCOUT_TEST_POSTGRES_URL")
        if not url:
            raise unittest.SkipTest("TRENDSCOUT_TEST_POSTGRES_URL is not configured")
        cls.environment = {"DATABASE_URL": url, "DATABASE_URL_UNPOOLED": url}
        cls.schema = "ts_test_" + uuid.uuid4().hex
        cls.manager = postgres_connection(cls.environment, direct=True)
        cls.connection = cls.manager.__enter__()
        cls.connection.autocommit = True
        assert not cls.connection.execute(
            "SELECT EXISTS (SELECT FROM pg_namespace WHERE nspname=%s)", (cls.schema,)
        ).fetchone()[0]
        try:
            apply_migrations(cls.connection, schema=cls.schema)
        except Exception:
            cls.manager.__exit__(None, None, None)
            raise

    @classmethod
    def tearDownClass(cls):
        try:
            cls.connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(cls.schema)))
        finally:
            cls.manager.__exit__(None, None, None)

    async def asyncSetUp(self):
        self.store = PostgresAnalysisStorage(self.environment, schema=self.schema)

    async def asyncTearDown(self):
        self.connection.execute(sql.SQL("TRUNCATE {}.analysis_records, {}.analysis_runs")
                                .format(sql.Identifier(self.schema), sql.Identifier(self.schema)))

    async def test_timestamp_precision_and_cursor_ties(self):
        stamp = AnalysisTimestamp(2026, 1, 1, nanosecond=123_456_789, tzinfo=timezone.utc)
        await self.store.create_run("a", {"created_at": stamp, "nullable": None, "zero": 0})
        await self.store.create_run("b", {"created_at": stamp})
        await self.store.create_run("without-date", {"status": "QUEUED"})
        await self.store.update_run("a", {"status": "COMPLETED"})
        row = await self.store.get_run("a")
        self.assertEqual(row["created_at"].nanosecond, 123_456_789)
        self.assertIsNone(row["nullable"])
        self.assertEqual(row["zero"], 0)
        first = await self.store.list_runs_after(limit=1)
        self.assertEqual(first[0]["run_id"], "b")
        second = await self.store.list_runs_after(limit=1, after_run_id="b")
        self.assertEqual(second[0]["run_id"], "a")
