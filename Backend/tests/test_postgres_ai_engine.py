"""PostgreSQL AI integration tests using disposable, isolated schemas."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
import uuid

import pytest
from psycopg import sql
from psycopg.types.json import Jsonb

from app.ai.openrouter import VerifiedModel
from app.ai.runtime import AIConfig, AIExecutionError, AIService, ROLLING_MINUTE_LIMIT
from app.collections.postgres_store import PostgresCollectionStore
from app.sources.catalog import WIKIMEDIA_SOURCE_ID
from app.storage.postgres_atomic_state import PostgresAtomicStateStore
from app.storage.postgres_connection import postgres_connection
from app.storage.postgres_migrations import apply_migrations


NOW = datetime(2026, 11, 26, 12, 0, tzinfo=timezone.utc)
MODEL_ID = "liquid/lfm-3-1.7b:free"


@contextmanager
def isolated_schema():
    url = os.environ.get("TRENDSCOUT_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("TRENDSCOUT_TEST_POSTGRES_URL is not configured")
    environment = {"DATABASE_URL": url, "DATABASE_URL_UNPOOLED": url}
    schema = "ts_test_" + uuid.uuid4().hex
    created = False
    with postgres_connection(environment, direct=True) as connection:
        connection.autocommit = True
        try:
            apply_migrations(connection, schema=schema)
            created = True
            yield environment, schema, connection
        finally:
            if created:
                connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


class ProposalThenAssignProvider:
    def __init__(self):
        self.calls = 0

    def verify_model(self, model_id):
        assert model_id == MODEL_ID
        return VerifiedModel(MODEL_ID, "Liquid", "liquid", 262_144, 8192, True)

    def complete(self, verified, *, messages, schema, max_tokens):
        self.calls += 1
        request = json.loads(messages[1]["content"])
        keywords = request["keywords"]
        collections = request["collections"]
        collection_id = collections[0]["id"] if collections else None
        payload = {
            "classifications": [{
                "keyword_id": item["id"], "collection_id": collection_id,
                "score": 0.94 if collection_id else 0.25,
                "reason": "The term fits the supplied topic context.",
            } for item in keywords],
            "proposals": [],
        }
        if not collections and len(keywords) >= 2:
            payload["proposals"].append({
                "name": "Outdoor recreation",
                "description": "Topics about recreation and equipment outdoors.",
                "keyword_ids": [item["id"] for item in keywords],
                "reason": "The terms describe a coherent outdoor activity topic.",
                "score": 0.81,
                "uncertainty": "This is a suggestion that needs user review.",
                "evidence_ids": sorted({ref for item in keywords for ref in item["evidence_ids"]}),
            })
        return json.dumps(payload), {"input_tokens": 12, "output_tokens": 9}, None


def _ai_config():
    return AIConfig(
        enabled=True,
        collection_model=MODEL_ID,
        recommendation_model=MODEL_ID,
        allowed_sources=frozenset({WIKIMEDIA_SOURCE_ID}),
        api_key_present=True,
    )


def test_postgres_ai_proposal_acceptance_and_targeted_reclassification():
    with isolated_schema() as (environment, schema, connection):
        store = PostgresCollectionStore(environment, schema=schema, data_mode_scope="LIVE")
        tables = {name: sql.Identifier(schema, name) for name in (
            "sources", "keywords", "source_evidence", "evidence_keywords",
        )}
        keyword_ids = ["ai-pg-keyword-1", "ai-pg-keyword-2"]
        evidence_ids = ["ai-pg-evidence-1", "ai-pg-evidence-2"]
        stamp = datetime(2026, 11, 25, tzinfo=timezone.utc)
        with connection.transaction():
            connection.execute(sql.SQL("""INSERT INTO {}(id,display_name,configuration)
                VALUES (%s,%s,%s) ON CONFLICT(id) DO NOTHING""").format(tables["sources"]),
                (WIKIMEDIA_SOURCE_ID, "Wikimedia", Jsonb({})))
            for index, (keyword_id, evidence_id) in enumerate(zip(keyword_ids, evidence_ids), start=1):
                connection.execute(sql.SQL("""INSERT INTO {}(id,keyword,normalized,source,project,
                    observed_at,data_mode,evidence,context) VALUES (%s,%s,%s,%s,%s,%s,'LIVE',%s,'')""")
                    .format(tables["keywords"]),
                    (keyword_id, f"Outdoor activity {index}", f"outdoor activity {index}",
                     WIKIMEDIA_SOURCE_ID, "en.wikipedia", "2026-11-25T00:00:00Z", Jsonb({})))
                connection.execute(sql.SQL("""INSERT INTO {}(id,source_id,content_digest,query_scope,
                    geography,observed_at,collected_at,payload) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""")
                    .format(tables["source_evidence"]),
                    (evidence_id, WIKIMEDIA_SOURCE_ID, f"digest-{index}", Jsonb({"scope": "fixture"}),
                     Jsonb({"country_code": "US"}), stamp, stamp, None))
                connection.execute(sql.SQL("""INSERT INTO {}(evidence_id,keyword_id)
                    VALUES (%s,%s)""").format(tables["evidence_keywords"]), (evidence_id, keyword_id))

        provider = ProposalThenAssignProvider()
        atomic = PostgresAtomicStateStore(environment, schema=schema)
        service = AIService(atomic, config=_ai_config(), provider=provider, utcnow=lambda: NOW)
        proposed = service.classify_keyword_ids(
            store, keyword_ids, idempotency_key="pg-proposal-integration",
        )
        assert proposed["status"] == "COMPLETED"
        assert len(proposed["proposal_ids"]) == 1
        proposal = store.suggestions()[0]
        assert proposal["evidence_ids"] == evidence_ids
        assert proposal["source_ids"] == [WIKIMEDIA_SOURCE_ID]

        collection_id = store.resolve_proposal(
            proposal["id"], True, name="Reviewed outdoor recreation",
            description="A user-reviewed topic for outdoor activities.",
            allowed_sources={WIKIMEDIA_SOURCE_ID},
        )
        assert collection_id
        accepted_collection = next(item for item in store.list_collections()
                                   if item["id"] == collection_id)
        assert accepted_collection["name"] == "Reviewed outdoor recreation"
        assert accepted_collection["description"] == "A user-reviewed topic for outdoor activities."
        assert {row["id"] for row in store.keywords_for(collection_id)} == set(keyword_ids)

        reclassified = service.classify_keyword_ids(
            store, keyword_ids, idempotency_key="pg-reclassify-integration",
        )
        assert reclassified["status"] == "COMPLETED"
        assert {row["keyword_id"] for row in reclassified["assigned"]} == set(keyword_ids)
        assert reclassified["membership_reconciliation_id"].startswith("reconcile-")
        assert provider.calls == 2
        assert connection.execute(sql.SQL("SELECT count(*) FROM {} WHERE origin='LOCAL_AI'")
                                   .format(sql.Identifier(schema, "memberships"))).fetchone()[0] == 2

        connection.execute(sql.SQL("UPDATE {} SET origin='USER',pinned=1,reason='User pinned' "
                                   "WHERE keyword_id=%s")
                            .format(sql.Identifier(schema, "memberships")), (keyword_ids[0],))
        protected = service.classify_keyword_ids(
            store, keyword_ids, idempotency_key="pg-manual-membership-protection",
        )
        assert protected["status"] == "COMPLETED"
        assert {row["keyword_id"] for row in protected["assigned"]} == {keyword_ids[1]}
        manual = connection.execute(sql.SQL("SELECT origin,pinned FROM {} WHERE keyword_id=%s")
                                    .format(sql.Identifier(schema, "memberships")),
                                    (keyword_ids[0],)).fetchone()
        assert tuple(manual) == ("USER", 1)


def test_postgres_ai_quota_is_shared_across_service_instances():
    with isolated_schema() as (environment, schema, _connection):
        services = [
            AIService(PostgresAtomicStateStore(environment, schema=schema),
                      config=_ai_config(), provider=object(), utcnow=lambda: NOW)
            for _ in range(2)
        ]

        def reserve(index):
            try:
                return services[index % len(services)]._reserve(f"shared-{index}", "classification")
            except AIExecutionError as exc:
                return exc.code

        with ThreadPoolExecutor(max_workers=8) as executor:
            outcomes = list(executor.map(reserve, range(ROLLING_MINUTE_LIMIT + 4)))

        assert sum(isinstance(value, str) and value.startswith("att_") for value in outcomes) == ROLLING_MINUTE_LIMIT
        assert outcomes.count("AI_RATE_LIMITED") == 4
        assert services[0]._quota()["daily_used"] == ROLLING_MINUTE_LIMIT
