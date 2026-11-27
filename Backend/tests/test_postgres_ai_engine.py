"""PostgreSQL AI integration tests using disposable, isolated schemas."""

from __future__ import annotations

import json
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest
from psycopg import sql
from psycopg.types.json import Jsonb

from app.ai.openrouter import VerifiedModel
from app.ai.runtime import (
    PUBLIC_DEMO,
    ROLLING_MINUTE_LIMIT,
    SYNTHETIC_AI_PROVENANCE_KEY,
    SYNTHETIC_AI_PROVENANCE_VALUE,
    AIConfig,
    AIExecutionError,
    AIService,
)
from app.collections.postgres_store import CollectionWriteError, PostgresCollectionStore
from app.sources.catalog import WIKIMEDIA_SOURCE_ID
from app.storage.postgres_atomic_state import PostgresAtomicStateStore
from app.storage.postgres_connection import postgres_connection
from app.storage.postgres_migrations import apply_migrations

NOW = datetime(2026, 11, 26, 12, 0, tzinfo=timezone.utc)
MODEL_ID = "liquid/lfm-3-1.7b:free"


@contextmanager
def isolated_schema(prefix="ts_test_"):
    url = os.environ.get("TRENDSCOUT_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("TRENDSCOUT_TEST_POSTGRES_URL is not configured")
    environment = {"DATABASE_URL": url, "DATABASE_URL_UNPOOLED": url}
    schema = prefix + uuid.uuid4().hex
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
        self.proposal_name = "Outdoor recreation"

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
                "name": self.proposal_name,
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
        transfer_approved_sources=frozenset({WIKIMEDIA_SOURCE_ID}),
        api_key_present=True,
    )


class RecommendationCaptureProvider:
    def __init__(self):
        self.inputs = []

    def verify_model(self, model_id):
        assert model_id == MODEL_ID
        return VerifiedModel(MODEL_ID, "Liquid", "liquid", 262_144, 8192, True)

    def complete(self, verified, *, messages, schema, max_tokens):
        payload = json.loads(messages[1]["content"])
        self.inputs.append(payload)
        keyword = payload["keywords"][0]
        response = {"recommendations": [{
            "product_name": "Handwoven tote",
            "product_category": "Textile accessories",
            "related_keyword_ids": [keyword["keyword_id"]],
            "supporting_evidence_ids": keyword["evidence_ids"][:1],
            "why_this_product": "A qualitative hypothesis for further export research.",
            "consumer_segment": "Shoppers interested in handcrafted accessories.",
            "seasonality": "Review seasonal context during research.",
            "possible_materials": ["Cotton canvas"],
            "manufacturing_considerations": ["Confirm supplier capability."],
            "export_considerations": ["Check current customs requirements."],
            "competition_risks": ["Review current US listings."],
            "ip_risks": ["Review relevant trademarks."],
            "regulatory_risks": ["Confirm current product requirements."],
            "uncertainty": "The available source evidence is limited and needs independent review.",
            "research_steps": ["Review current US marketplace listings."],
        }]}
        return json.dumps(response), {"input_tokens": 12, "output_tokens": 9}, None


def _seed_proposal_keywords(connection, schema, count=3):
    tables = {name: sql.Identifier(schema, name) for name in (
        "sources", "keywords", "source_evidence", "evidence_keywords",
    )}
    keyword_ids = [f"proposal-keyword-{index}" for index in range(1, count + 1)]
    stamp = NOW - timedelta(days=1)
    with connection.transaction():
        connection.execute(sql.SQL("""INSERT INTO {}(id,display_name,configuration)
            VALUES (%s,%s,%s) ON CONFLICT(id) DO NOTHING""").format(tables["sources"]),
            (WIKIMEDIA_SOURCE_ID, "Wikimedia", Jsonb({})))
        for index, keyword_id in enumerate(keyword_ids, start=1):
            evidence_id = f"proposal-evidence-{index}"
            connection.execute(sql.SQL("""INSERT INTO {}(id,keyword,normalized,source,project,
                observed_at,data_mode,evidence,context) VALUES (%s,%s,%s,%s,%s,%s,'LIVE',%s,'')""")
                .format(tables["keywords"]),
                (keyword_id, f"Outdoor activity {index}", f"outdoor activity {index}",
                 WIKIMEDIA_SOURCE_ID, "en.wikipedia", stamp.isoformat(), Jsonb({})))
            connection.execute(sql.SQL("""INSERT INTO {}(id,source_id,content_digest,query_scope,
                geography,observed_at,collected_at,expires_at,payload)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""").format(tables["source_evidence"]),
                (evidence_id, WIKIMEDIA_SOURCE_ID, f"proposal-digest-{index}",
                 Jsonb({"data_mode": "LIVE"}), Jsonb({"country_code": "US"}), stamp,
                 stamp, NOW + timedelta(days=7), None))
            connection.execute(sql.SQL("""INSERT INTO {}(evidence_id,keyword_id)
                VALUES (%s,%s)""").format(tables["evidence_keywords"]),
                (evidence_id, keyword_id))
    return keyword_ids


def _propose(store, environment, provider, keyword_ids, key):
    service = AIService(
        PostgresAtomicStateStore(environment, schema=store.schema),
        config=_ai_config(), provider=provider, utcnow=lambda: NOW,
    )
    result = service.classify_keyword_ids(store, keyword_ids, idempotency_key=key)
    assert result["status"] == "COMPLETED", (result.get("status"), result.get("error_code"))
    assert len(result["proposal_ids"]) == 1
    return service, result, store.get_proposal(result["proposal_ids"][0])


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
                    geography,observed_at,collected_at,expires_at,payload)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""")
                    .format(tables["source_evidence"]),
                    (evidence_id, WIKIMEDIA_SOURCE_ID, f"digest-{index}", Jsonb({"scope": "fixture"}),
                     Jsonb({"country_code": "US"}), stamp, stamp,
                     stamp + timedelta(days=7), None))
                connection.execute(sql.SQL("""INSERT INTO {}(evidence_id,keyword_id)
                    VALUES (%s,%s)""").format(tables["evidence_keywords"]), (evidence_id, keyword_id))
            for index in range(20):
                evidence_id = f"ai-pg-evidence-extra-{index:02d}"
                connection.execute(sql.SQL("""INSERT INTO {}(id,source_id,content_digest,query_scope,
                    geography,observed_at,collected_at,expires_at,payload)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""").format(tables["source_evidence"]),
                    (evidence_id, WIKIMEDIA_SOURCE_ID, f"extra-digest-{index}",
                     Jsonb({"scope": "fixture"}), Jsonb({"country_code": "US"}), stamp,
                     stamp + timedelta(seconds=index), stamp + timedelta(days=7), None))
                connection.execute(sql.SQL("""INSERT INTO {}(evidence_id,keyword_id)
                    VALUES (%s,%s)""").format(tables["evidence_keywords"]),
                    (evidence_id, keyword_ids[0]))

        provider = ProposalThenAssignProvider()
        atomic = PostgresAtomicStateStore(environment, schema=schema)
        service = AIService(atomic, config=_ai_config(), provider=provider, utcnow=lambda: NOW)
        proposed = service.classify_keyword_ids(
            store, keyword_ids, idempotency_key="pg-proposal-integration",
        )
        assert proposed["status"] == "COMPLETED", (
            proposed.get("status"), proposed.get("error_code"), proposed.get("processed"),
        )
        assert len(proposed["proposal_ids"]) == 1
        proposal = store.suggestions()[0]
        assert len(proposal["evidence_ids"]) == 21
        assert "ai-pg-evidence-1" in proposal["evidence_ids"]
        assert "ai-pg-evidence-extra-00" not in proposal["evidence_ids"]
        assert "ai-pg-evidence-extra-19" in proposal["evidence_ids"]
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


def test_postgres_proposal_edit_and_concurrent_accept_are_atomic_and_idempotent():
    with isolated_schema() as (environment, schema, connection):
        keyword_ids = _seed_proposal_keywords(connection, schema, count=3)
        store = PostgresCollectionStore(environment, schema=schema, data_mode_scope="LIVE")
        provider = ProposalThenAssignProvider()
        _service, _result, proposal = _propose(
            store, environment, provider, keyword_ids, "pg-proposal-edit-accept",
        )
        assert proposal["job_id"]
        assert proposal["expires_at"]
        assert proposal["candidate_keyword_ids"] == keyword_ids
        assert proposal["support_ids"] == keyword_ids

        assert store.patch_proposal(
            proposal["id"], name="Edited outdoor topic", description="User reviewed description.",
            support_ids=keyword_ids[:2], allowed_sources={WIKIMEDIA_SOURCE_ID},
        )
        edited = store.get_proposal(proposal["id"])
        assert edited["support_ids"] == keyword_ids[:2]
        assert edited["candidate_keyword_ids"] == keyword_ids
        assert len(edited["evidence_ids"]) == 2

        def accept():
            return store.resolve_proposal(
                proposal["id"], True, allowed_sources={WIKIMEDIA_SOURCE_ID},
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            accepted_ids = list(pool.map(lambda _index: accept(), range(2)))
        assert accepted_ids[0] == accepted_ids[1]
        accepted_id = accepted_ids[0]
        persisted = store.get_proposal(proposal["id"])
        assert persisted["status"] == "ACCEPTED"
        assert persisted["accepted_collection_id"] == accepted_id
        assert {row["id"] for row in store.keywords_for(accepted_id)} == set(keyword_ids[:2])
        assert connection.execute(sql.SQL("SELECT count(*) FROM {} WHERE id=%s")
            .format(sql.Identifier(schema, "collections")), (accepted_id,)).fetchone()[0] == 1
        assert connection.execute(sql.SQL("SELECT count(*) FROM {} WHERE collection_id=%s")
            .format(sql.Identifier(schema, "memberships")), (accepted_id,)).fetchone()[0] == 2


def test_postgres_new_collection_during_inference_invalidates_proposal_context():
    with isolated_schema() as (environment, schema, connection):
        keyword_ids = _seed_proposal_keywords(connection, schema, count=2)
        store = PostgresCollectionStore(environment, schema=schema, data_mode_scope="LIVE")

        class AddCollectionDuringInference(ProposalThenAssignProvider):
            def __init__(self):
                super().__init__()
                self.inserted = False
                self.db_schema = schema
                self.db_connection = connection

            def complete(self, verified, *, messages, schema, max_tokens):
                output = super().complete(verified, messages=messages, schema=schema,
                                          max_tokens=max_tokens)
                if not self.inserted:
                    stamp = datetime.now(timezone.utc)
                    self.db_connection.execute(sql.SQL("""INSERT INTO {}(id,name,description,created_by,
                        reason,created_at,updated_at) VALUES ('concurrent-collection',
                        'Outdoor recreation','', 'USER','',%s,%s)""").format(
                            sql.Identifier(self.db_schema, "collections")), (stamp, stamp))
                    self.inserted = True
                return output

        provider = AddCollectionDuringInference()
        service = AIService(
            PostgresAtomicStateStore(environment, schema=schema),
            config=_ai_config(), provider=provider, utcnow=lambda: NOW,
        )
        result = service.classify_keyword_ids(
            store, keyword_ids, idempotency_key="pg-proposal-context-race",
        )
        assert result["status"] == "FAILED"
        assert result["error_code"] == "AI_INPUT_CHANGED"
        assert store.suggestions() == []
        assert store.list_collections()[0]["id"] == "concurrent-collection"


def test_postgres_expired_proposal_and_new_user_membership_block_acceptance():
    with isolated_schema() as (environment, schema, connection):
        keyword_ids = _seed_proposal_keywords(connection, schema, count=2)
        store = PostgresCollectionStore(environment, schema=schema, data_mode_scope="LIVE")
        provider = ProposalThenAssignProvider()
        _service, _result, proposal = _propose(
            store, environment, provider, keyword_ids, "pg-proposal-expiry",
        )
        connection.execute(sql.SQL("UPDATE {} SET expires_at=%s WHERE id=%s")
            .format(sql.Identifier(schema, "proposals")),
            (NOW - timedelta(days=1), proposal["id"]))
        stored_expiry = connection.execute(sql.SQL("SELECT expires_at FROM {} WHERE id=%s")
            .format(sql.Identifier(schema, "proposals")), (proposal["id"],)).fetchone()[0]
        assert stored_expiry <= datetime.now(timezone.utc)
        with pytest.raises(ValueError, match="AI_PROPOSAL_EXPIRED"):
            store.resolve_proposal(proposal["id"], True, allowed_sources={WIKIMEDIA_SOURCE_ID})
        assert store.get_proposal(proposal["id"])["status"] == "PENDING"

    with isolated_schema() as (environment, schema, connection):
        keyword_ids = _seed_proposal_keywords(connection, schema, count=2)
        store = PostgresCollectionStore(environment, schema=schema, data_mode_scope="LIVE")
        provider = ProposalThenAssignProvider()
        _service, _result, proposal = _propose(
            store, environment, provider, keyword_ids, "pg-proposal-user-edit",
        )
        stamp = datetime.now(timezone.utc)
        connection.execute(sql.SQL("""INSERT INTO {}(id,name,description,created_by,reason,created_at,updated_at)
            VALUES ('user-collection','User Collection','', 'USER','',%s,%s)""").format(
                sql.Identifier(schema, "collections")), (stamp, stamp))
        connection.execute(sql.SQL("""INSERT INTO {}(collection_id,keyword_id,origin,pinned,reason,created_at)
            VALUES ('user-collection',%s,'USER',1,'User pinned after proposal generation',%s)""").format(
                sql.Identifier(schema, "memberships")), (keyword_ids[0], stamp))
        with pytest.raises(ValueError, match="AI_INPUT_CHANGED"):
            store.resolve_proposal(proposal["id"], True, allowed_sources={WIKIMEDIA_SOURCE_ID})
        assert connection.execute(sql.SQL("SELECT origin,pinned FROM {} WHERE collection_id='user-collection' "
                                          "AND keyword_id=%s").format(sql.Identifier(schema, "memberships")),
                                   (keyword_ids[0],)).fetchone() == ("USER", 1)
        assert store.get_proposal(proposal["id"])["status"] == "PENDING"

    with isolated_schema() as (environment, schema, connection):
        keyword_ids = _seed_proposal_keywords(connection, schema, count=2)
        store = PostgresCollectionStore(environment, schema=schema, data_mode_scope="LIVE")
        provider = ProposalThenAssignProvider()
        _service, _result, proposal = _propose(
            store, environment, provider, keyword_ids, "pg-proposal-expired-evidence",
        )
        connection.execute(sql.SQL("""UPDATE {} SET expires_at=%s WHERE id IN (
            SELECT evidence_id FROM {} WHERE keyword_id=ANY(%s))""").format(
                sql.Identifier(schema, "source_evidence"),
                sql.Identifier(schema, "evidence_keywords")),
            (NOW - timedelta(days=1), keyword_ids))
        with pytest.raises(ValueError, match="AI_PROPOSAL_EXPIRED"):
            store.resolve_proposal(proposal["id"], True, allowed_sources={WIKIMEDIA_SOURCE_ID})
        assert store.get_proposal(proposal["id"])["status"] == "PENDING"


def test_postgres_rejected_proposal_suppresses_same_keyword_content():
    with isolated_schema() as (environment, schema, connection):
        keyword_ids = _seed_proposal_keywords(connection, schema, count=2)
        store = PostgresCollectionStore(environment, schema=schema, data_mode_scope="LIVE")
        provider = ProposalThenAssignProvider()
        service, _result, proposal = _propose(
            store, environment, provider, keyword_ids, "pg-proposal-reject",
        )
        assert store.resolve_proposal(proposal["id"], False) == ""
        assert store.suggestions() == []
        provider.proposal_name = "Camping equipment"
        connection.execute(sql.SQL("UPDATE {} SET normalized=normalized || ' revised' WHERE id=ANY(%s)")
            .format(sql.Identifier(schema, "keywords")), (keyword_ids,))

        again = service.classify_keyword_ids(
            store, keyword_ids, idempotency_key="pg-proposal-reject-again",
        )
        assert again["status"] == "COMPLETED"
        assert again["proposal_ids"] == []
        assert store.suggestions() == []
        assert provider.calls == 2
        assert connection.execute(sql.SQL("SELECT count(*) FROM {} WHERE kind='REJECTED_PROPOSAL_CONTENT'")
            .format(sql.Identifier(schema, "suppression"))).fetchone()[0] == 1


def test_postgres_pending_proposal_deduplicates_identical_keyword_content():
    with isolated_schema() as (environment, schema, connection):
        keyword_ids = _seed_proposal_keywords(connection, schema, count=2)
        store = PostgresCollectionStore(environment, schema=schema, data_mode_scope="LIVE")
        provider = ProposalThenAssignProvider()
        service, _first, proposal = _propose(
            store, environment, provider, keyword_ids, "pg-proposal-pending-dedupe-one",
        )
        provider.proposal_name = "Camping equipment"
        connection.execute(sql.SQL("UPDATE {} SET normalized=normalized || ' revised' WHERE id=ANY(%s)")
            .format(sql.Identifier(schema, "keywords")), (keyword_ids,))

        second = service.classify_keyword_ids(
            store, keyword_ids, idempotency_key="pg-proposal-pending-dedupe-two",
        )
        assert second["status"] == "COMPLETED"
        assert second["proposal_ids"] == []
        pending = store.suggestions()
        assert [item["id"] for item in pending] == [proposal["id"]]
        assert pending[0]["support_ids"] == keyword_ids
        assert provider.calls == 2


def test_postgres_proposal_accept_rolls_back_on_membership_insert_failure():
    with isolated_schema() as (environment, schema, connection):
        keyword_ids = _seed_proposal_keywords(connection, schema, count=2)
        store = PostgresCollectionStore(environment, schema=schema, data_mode_scope="LIVE")
        provider = ProposalThenAssignProvider()
        _service, _result, proposal = _propose(
            store, environment, provider, keyword_ids, "pg-proposal-rollback",
        )
        connection.execute(sql.SQL("""CREATE FUNCTION {}() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                IF NEW.reason='Representative keyword in accepted AI suggestion' THEN
                    RAISE EXCEPTION 'simulated proposal membership failure';
                END IF;
                RETURN NEW;
            END;
            $$""").format(sql.Identifier(schema, "reject_proposal_membership")))
        connection.execute(sql.SQL("""CREATE TRIGGER reject_proposal_membership
            BEFORE INSERT ON {} FOR EACH ROW EXECUTE FUNCTION {}()""").format(
                sql.Identifier(schema, "memberships"),
                sql.Identifier(schema, "reject_proposal_membership")))

        with pytest.raises(CollectionWriteError):
            store.resolve_proposal(proposal["id"], True, allowed_sources={WIKIMEDIA_SOURCE_ID})
        assert store.get_proposal(proposal["id"])["status"] == "PENDING"
        assert store.list_collections() == []
        assert connection.execute(sql.SQL("SELECT count(*) FROM {} WHERE origin='LOCAL_AI'")
            .format(sql.Identifier(schema, "memberships"))).fetchone()[0] == 0


def test_postgres_ai_assignment_revalidates_a_concurrent_user_pin():
    with isolated_schema() as (environment, schema, connection):
        store = PostgresCollectionStore(environment, schema=schema, data_mode_scope="LIVE")
        tables = {name: sql.Identifier(schema, name) for name in (
            "sources", "keywords", "source_evidence", "evidence_keywords", "memberships",
        )}
        keyword_id, evidence_id = "concurrent-pin-keyword", "concurrent-pin-evidence"
        collection_id = store.create({"name": "Concurrent pin target"})
        stamp = datetime(2026, 11, 25, tzinfo=timezone.utc)
        connection.execute(sql.SQL("INSERT INTO {}(id,display_name,configuration) "
            "VALUES (%s,%s,%s) ON CONFLICT(id) DO NOTHING").format(tables["sources"]),
            (WIKIMEDIA_SOURCE_ID, "Wikimedia", Jsonb({})))
        connection.execute(sql.SQL("""INSERT INTO {}(id,keyword,normalized,source,project,
            observed_at,data_mode,evidence,context) VALUES (%s,%s,%s,%s,%s,%s,'LIVE',%s,'')""")
            .format(tables["keywords"]),
            (keyword_id, "Portable solar generator", "portable solar generator",
             WIKIMEDIA_SOURCE_ID, "en.wikipedia", "2026-11-25T00:00:00Z", Jsonb({})))
        connection.execute(sql.SQL("""INSERT INTO {}(id,source_id,content_digest,query_scope,
            geography,observed_at,collected_at,expires_at,payload)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""").format(tables["source_evidence"]),
            (evidence_id, WIKIMEDIA_SOURCE_ID, "concurrent-pin-digest", Jsonb({"scope": "fixture"}),
             Jsonb({"country_code": "US"}), stamp, stamp, stamp + timedelta(days=7), None))
        connection.execute(sql.SQL("INSERT INTO {}(evidence_id,keyword_id) VALUES (%s,%s)")
            .format(tables["evidence_keywords"]), (evidence_id, keyword_id))

        class ConcurrentPinProvider:
            calls = 0

            def verify_model(self, model_id):
                assert model_id == MODEL_ID
                return VerifiedModel(MODEL_ID, "Liquid", "liquid", 262_144, 8192, True)

            def complete(self, verified, *, messages, schema, max_tokens):
                self.calls += 1
                collection = json.loads(messages[1]["content"])["collections"][0]
                connection.execute(sql.SQL("""INSERT INTO {}(collection_id,keyword_id,origin,
                    pinned,score,reason,created_at) VALUES (%s,%s,'USER',1,NULL,'Concurrent pin',%s)""")
                    .format(tables["memberships"]), (collection_id, keyword_id, stamp))
                keyword = json.loads(messages[1]["content"])["keywords"][0]
                return json.dumps({"classifications": [{
                    "keyword_id": keyword["id"], "collection_id": collection["id"],
                    "score": 0.94, "reason": "The keyword fits this topic.",
                }], "proposals": []}), {"input_tokens": 12, "output_tokens": 9}, None

            @staticmethod
            def key_quota_metadata():
                return {"is_free_tier": True, "limit_remaining": 40.0}

        provider = ConcurrentPinProvider()
        service = AIService(PostgresAtomicStateStore(environment, schema=schema),
                            config=_ai_config(), provider=provider, utcnow=lambda: NOW)
        result = service.classify_keyword_ids(
            store, [keyword_id], job_id="concurrent-user-pin", automatic=True,
        )
        assert result["error_code"] == "AI_INPUT_CHANGED"
        assert result["assigned"] == []
        membership = connection.execute(sql.SQL("SELECT origin,pinned FROM {} WHERE keyword_id=%s")
            .format(tables["memberships"]), (keyword_id,)).fetchone()
        assert tuple(membership) == ("USER", 1)
        assert connection.execute(sql.SQL("SELECT count(*) FROM {} WHERE keyword_id=%s "
            "AND origin='LOCAL_AI'").format(tables["memberships"]), (keyword_id,)).fetchone()[0] == 0


def test_postgres_ai_recommendation_uses_active_lineage_and_bounded_algorithm_profile():
    with isolated_schema() as (environment, schema, connection):
        store = PostgresCollectionStore(environment, schema=schema, data_mode_scope="LIVE")
        tables = {name: sql.Identifier(schema, name) for name in (
            "sources", "keywords", "source_evidence", "evidence_keywords",
            "classification_results", "algorithm_results",
        )}
        keyword_id = "ai-recommendation-keyword"
        active_evidence_id = "ai-recommendation-active-evidence"
        expired_evidence_id = "ai-recommendation-expired-evidence"
        now = datetime.now(timezone.utc)
        observed = now - timedelta(hours=1)
        with connection.transaction():
            connection.execute(sql.SQL("INSERT INTO {}(id,display_name,configuration) VALUES (%s,%s,%s)")
                               .format(tables["sources"]),
                               (WIKIMEDIA_SOURCE_ID, "Wikimedia", Jsonb({})))
            connection.execute(sql.SQL("""INSERT INTO {}(id,keyword,normalized,source,project,
                observed_at,data_mode,evidence) VALUES (%s,%s,%s,%s,%s,%s,'LIVE',%s)""")
                .format(tables["keywords"]),
                (keyword_id, "Handwoven tote", "handwoven tote", WIKIMEDIA_SOURCE_ID,
                 "en.wikipedia", observed.isoformat(), Jsonb({"views_ceil": 23})))
            connection.execute(sql.SQL("""INSERT INTO {}(keyword_id,entity_type,topic_category_ids,
                classification_status,confidence_level,classification_method,classification_version,
                taxonomy_version,review_status,classified_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""")
                .format(tables["classification_results"]),
                (keyword_id, "product", Jsonb(["products_brands"]), "CLASSIFIED", "HIGH",
                 "rules", "test-v1", "taxonomy-test-v1", "UNREVIEWED", now))
            for evidence_id, expires_at in (
                (active_evidence_id, now + timedelta(days=5)),
                (expired_evidence_id, now - timedelta(minutes=1)),
            ):
                scope = {
                    "data_mode": "LIVE", "country_scope": "US",
                    "source_metric_name": "views_ceil",
                    "source_metric_unit": "approx_pageviews_upper_bound",
                }
                connection.execute(sql.SQL("""INSERT INTO {}(id,source_id,content_digest,query_scope,
                    geography,observed_at,collected_at,expires_at,payload)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""")
                    .format(tables["source_evidence"]),
                    (evidence_id, WIKIMEDIA_SOURCE_ID, "digest-" + evidence_id, Jsonb(scope),
                     Jsonb({"country_code": "US", "field": "country_scope", "value": "US"}),
                     observed, observed, expires_at, Jsonb({"raw": "PRIVATE_RAW_PAYLOAD_SENTINEL"})))
                connection.execute(sql.SQL("INSERT INTO {}(evidence_id,keyword_id) VALUES (%s,%s)")
                                   .format(tables["evidence_keywords"]), (evidence_id, keyword_id))
            algorithm_result = {
                "source_id": WIKIMEDIA_SOURCE_ID,
                "window_days": 7,
                "evidence_references": [{
                    "source_evidence_id": active_evidence_id,
                    "source_id": WIKIMEDIA_SOURCE_ID,
                    "metric": "views_ceil",
                    "unit": "approx_pageviews_upper_bound",
                }],
            }
            connection.execute(sql.SQL("""INSERT INTO {}(keyword_id,algorithm_id,version,status,
                reason_code,value,metric,unit,result_json,input_digest,updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""")
                .format(tables["algorithm_results"]),
                (keyword_id, "observed-volume-rank", "v1", "COMPLETED", "OBSERVED",
                 1.0, "provider_rank", "ordinal_top_list_position",
                 json.dumps(algorithm_result), "rank-input-digest", now))

        context = store.ai_recommendation_context(
            [keyword_id], allowed_sources={WIKIMEDIA_SOURCE_ID},
        )
        record = context["keywords"][0]
        assert record["categories"] == ["products_brands"]
        assert record["evidence_ids"] == [active_evidence_id]
        assert expired_evidence_id not in record["evidence_expiry"]
        assert record["algorithm_results"][0]["input_digest"] == "rank-input-digest"
        assert "PRIVATE_RAW_PAYLOAD_SENTINEL" not in repr(record)

        provider = RecommendationCaptureProvider()
        service = AIService(PostgresAtomicStateStore(environment, schema=schema),
                            config=_ai_config(), provider=provider, utcnow=lambda: now)
        result = service.recommend(store, [keyword_id], idempotency_key="pg-recommendation-contract")
        assert result["status"] == "COMPLETED"
        payload_keyword = provider.inputs[0]["keywords"][0]
        assert payload_keyword["durable_keyword_ids"] == [keyword_id]
        assert payload_keyword["categories"] == ["products_brands"]
        assert payload_keyword["evidence_ids"] == [active_evidence_id]
        assert payload_keyword["source_evidence_profile"][0]["evidence_references"][0][
            "evidence_role"] == "OBSERVED_ACTIVITY"
        ranking = payload_keyword["source_evidence_profile"][0]["source_specific_results"][0]
        assert ranking["input_digest"] == "rank-input-digest"
        assert ranking["source_scope"]["window_days"] == 7
        assert payload_keyword["market_relevance"]["status"] == "US_COUNTRY_SCOPED"
        assert payload_keyword["multi_source_corroboration"]["composite_score"] is None
        assert "PRIVATE_RAW_PAYLOAD_SENTINEL" not in json.dumps(provider.inputs)


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


def test_postgres_provider_rate_limit_cooldown_is_shared_across_tasks():
    with isolated_schema() as (environment, schema, _connection):
        services = [
            AIService(PostgresAtomicStateStore(environment, schema=schema),
                      config=_ai_config(), provider=object(), utcnow=lambda: NOW)
            for _ in range(2)
        ]
        attempt = services[0]._reserve("provider-limited-classification", "classification")
        retry_at = (NOW + timedelta(minutes=1)).isoformat()
        services[0]._finish_attempt(
            attempt, outcome="FAILED", error_code="AI_RATE_LIMITED", retry_at=retry_at,
        )
        with pytest.raises(AIExecutionError) as caught:
            services[1]._reserve("provider-limited-recommendation", "recommendations")

        assert caught.value.code == "AI_RATE_LIMITED"
        assert caught.value.retry_at == retry_at
        assert services[0]._quota()["daily_used"] == 1


def test_public_demo_profile_is_bound_to_attested_synthetic_postgres_schema():
    with isolated_schema("ts_test_ai_demo_") as (environment, schema, connection):
        store = PostgresCollectionStore(environment, schema=schema, data_mode_scope="SYNTHETIC")
        store.set_setting(SYNTHETIC_AI_PROVENANCE_KEY, SYNTHETIC_AI_PROVENANCE_VALUE)
        atomic = PostgresAtomicStateStore(environment, schema=schema)
        service = AIService(atomic, config=AIConfig(
            enabled=True,
            collection_model=MODEL_ID,
            recommendation_model=MODEL_ID,
            api_key_present=True,
            data_profile=PUBLIC_DEMO,
            synthetic_schema=schema,
        ), provider=object(), utcnow=lambda: NOW)
        service._assert_profile_store(store)
        assert store.schema == atomic.schema == schema
        assert store.get_setting(SYNTHETIC_AI_PROVENANCE_KEY) == SYNTHETIC_AI_PROVENANCE_VALUE

        sources = sql.Identifier(schema, "sources")
        keywords = sql.Identifier(schema, "keywords")
        with connection.transaction():
            connection.execute(sql.SQL("""INSERT INTO {}(id,display_name,configuration)
                VALUES (%s,%s,%s) ON CONFLICT(id) DO NOTHING""").format(sources),
                (WIKIMEDIA_SOURCE_ID, "Synthetic fixture source", Jsonb({})))
            for keyword_id, mode in (("demo-synthetic", "SYNTHETIC"), ("demo-live", "LIVE")):
                connection.execute(sql.SQL("""INSERT INTO {}(id,keyword,normalized,source,project,
                    observed_at,data_mode,evidence,context) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'')""")
                    .format(keywords),
                    (keyword_id, f"Fixture {mode}", f"fixture {mode.casefold()}",
                     WIKIMEDIA_SOURCE_ID, "synthetic", NOW.isoformat(), mode, Jsonb({})))

        context = store.ai_classification_context(
            ["demo-synthetic"], allowed_sources={WIKIMEDIA_SOURCE_ID},
        )
        assert [item["id"] for item in context["keywords"]] == ["demo-synthetic"]
        assert context["keywords"][0]["data_mode"] == "SYNTHETIC"
        with pytest.raises(ValueError, match="AI_KEYWORD_NOT_FOUND"):
            store.ai_classification_context(
                ["demo-live"], allowed_sources={WIKIMEDIA_SOURCE_ID},
            )

        invalid_store = PostgresCollectionStore(
            environment, schema=schema, data_mode_scope="SYNTHETIC",
        )
        invalid_store.set_setting(SYNTHETIC_AI_PROVENANCE_KEY, "client-supplied")
        with pytest.raises(AIExecutionError) as caught:
            service._assert_profile_store(invalid_store)
        assert caught.value.code == "AI_POLICY_DENIED"
