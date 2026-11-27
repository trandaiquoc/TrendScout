"""AI08 end-to-end acceptance using synthetic inputs and isolated PostgreSQL."""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone

import pytest
from psycopg import sql

from app.ai.openrouter import VerifiedModel
from app.ai.runtime import (
    PUBLIC_DEMO,
    SYNTHETIC_AI_PROVENANCE_KEY,
    SYNTHETIC_AI_PROVENANCE_VALUE,
    AIConfig,
    AIService,
)
from app.collections.postgres_store import PostgresCollectionStore
from app.collections.service import CollectionService
from app.sources.catalog import WIKIMEDIA_SOURCE_ID
from app.storage.postgres_atomic_state import PostgresAtomicStateStore
from app.storage.postgres_connection import postgres_connection
from app.storage.postgres_migrations import apply_migrations

MODEL_ID = "liquid/lfm-3-1.7b:free"
ACCEPTED_COLLECTION_NAME = "AI08 reviewed synthetic collection"


class ConsolidatedAcceptanceProvider:
    """Deterministic structured-output fixture; never contacts a provider."""

    data_profile = PUBLIC_DEMO

    def __init__(self):
        self.classification_calls = 0
        self.recommendation_calls = 0
        self.requests = []

    def verify_model(self, model_id):
        assert model_id == MODEL_ID
        return VerifiedModel(MODEL_ID, "Liquid", "liquid", 262_144, 8192, True)

    def complete(self, _verified, *, messages, schema, max_tokens):
        del schema, max_tokens
        payload = json.loads(messages[1]["content"])
        keywords = payload["keywords"]
        if keywords and "keyword_id" in keywords[0]:
            self.recommendation_calls += 1
            self.requests.append(("recommendation", payload))
            keyword = keywords[0]
            result = {"recommendations": [{
                "product_name": "Woven utility pouch",
                "product_category": "Textile accessories",
                "related_keyword_ids": [keyword["keyword_id"]],
                "supporting_evidence_ids": keyword["evidence_ids"][:1],
                "why_this_product": "A qualitative hypothesis for further export research.",
                "consumer_segment": "Shoppers researching practical textile accessories.",
                "seasonality": "Check seasonality against current market evidence.",
                "possible_materials": ["Cotton canvas"],
                "manufacturing_considerations": ["Confirm supplier capability."],
                "export_considerations": ["Confirm current customs requirements."],
                "competition_risks": ["Review current US listings."],
                "ip_risks": ["Review relevant trademarks."],
                "regulatory_risks": ["Confirm current product requirements."],
                "uncertainty": "Synthetic evidence and a mocked model do not validate demand.",
                "research_steps": ["Review current US marketplace listings."],
            }]}
            return json.dumps(result), {"input_tokens": 12, "output_tokens": 9}, None

        self.classification_calls += 1
        self.requests.append(("classification", payload))
        ids = [item["id"] for item in keywords]
        texts = [str(item.get("display_keyword", "")).casefold() for item in keywords]
        collections = payload["collections"]
        is_rejection = len(keywords) == 2 and all("reject" in text for text in texts)
        is_preview = len(keywords) == 2 and all("preview" in text for text in texts)
        target = next((item for item in collections
                       if item["name"] == ACCEPTED_COLLECTION_NAME), None)
        classifications = [{
            "keyword_id": item["id"],
            "collection_id": target["id"] if is_preview and target else None,
            "score": 0.94 if is_preview and target else 0.25,
            "reason": "Synthetic acceptance fixture classification.",
        } for item in keywords]

        proposals = []
        if not is_preview:
            proposals.append({
                "name": "AI08 rejected synthetic candidate" if is_rejection
                        else "AI08 initial synthetic candidate",
                "description": "A synthetic proposal used only to exercise review state.",
                "keyword_ids": ids,
                "reason": "A bounded mock proposal that requires user review.",
                "score": 0.81,
                "uncertainty": "Fixture output; no market or model-quality claim.",
                "evidence_ids": sorted({evidence_id for item in keywords
                                         for evidence_id in item["evidence_ids"]}),
            })
        return json.dumps({"classifications": classifications, "proposals": proposals}), {
            "input_tokens": 12, "output_tokens": 9,
        }, None


def test_ai08_fetch_to_recommendation_acceptance_in_isolated_postgres():
    url = os.environ.get("TRENDSCOUT_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("TRENDSCOUT_TEST_POSTGRES_URL is not configured")

    environment = {"DATABASE_URL": url, "DATABASE_URL_UNPOOLED": url}
    schema = "ts_test_ai_demo_" + uuid.uuid4().hex
    now = datetime.now(timezone.utc)
    provider = ConsolidatedAcceptanceProvider()
    with postgres_connection(environment, direct=True) as connection:
        connection.autocommit = True
        try:
            apply_migrations(connection, schema=schema)
            store = PostgresCollectionStore(environment, schema=schema, data_mode_scope="SYNTHETIC")
            store.set_setting(SYNTHETIC_AI_PROVENANCE_KEY, SYNTHETIC_AI_PROVENANCE_VALUE)
            config = AIConfig(
                enabled=True,
                collection_model=MODEL_ID,
                recommendation_model=MODEL_ID,
                api_key_present=True,
                data_profile=PUBLIC_DEMO,
                synthetic_schema=schema,
            )
            ai_service = AIService(
                PostgresAtomicStateStore(environment, schema=schema),
                config=config,
                provider=provider,
                utcnow=lambda: now,
            )
            collection_service = CollectionService(
                store, fetch_model_inference_enabled=True, ai_engine=ai_service,
            )

            labels = [
                "AI08 accepted synthetic item one", "AI08 accepted synthetic item two",
                "AI08 reject synthetic item one", "AI08 reject synthetic item two",
                "AI08 preview synthetic item one", "AI08 preview synthetic item two",
            ]
            records = [{
                "article_display": label,
                "article_original": label.replace(" ", "_"),
                "project": "en.wikipedia",
                "observation_date": now.date().isoformat(),
                "fetched_at": now.isoformat(),
                "views_ceil": 100 + index,
                "provider_rank": index,
            } for index, label in enumerate(labels, start=1)]
            source = {
                "source_id": WIKIMEDIA_SOURCE_ID,
                "status": "SUCCESS",
                "data_mode": "SYNTHETIC",
                "delivery_state": "FIXTURE",
                "record_count": len(records),
                "records": records,
            }
            fetch_id = "ai08-synthetic-fetch-" + uuid.uuid4().hex
            fetched = collection_service._process_fetch(fetch_id, [source])
            assert fetched["status"] == "COMPLETED", {
                key: fetched.get(key) for key in (
                    "status", "error_code", "processed", "suggestions", "ai_job_ids",
                )
            }
            assert fetched["suggestions"] == 1
            assert provider.classification_calls == 1

            keyword_rows = connection.execute(sql.SQL(
                "SELECT id,keyword,data_mode FROM {} WHERE keyword LIKE 'AI08 %' ORDER BY keyword"
            ).format(sql.Identifier(schema, "keywords"))).fetchall()
            by_label = {row[1]: row[0] for row in keyword_rows}
            assert set(by_label) == set(labels)
            assert {row[2] for row in keyword_rows} == {"SYNTHETIC"}
            keyword_ids = [by_label[label] for label in labels]

            for keyword_id in keyword_ids:
                evidence = store.keyword_evidence_rows(
                    source_id=WIKIMEDIA_SOURCE_ID, data_mode="SYNTHETIC",
                )
                assert any(item["id"] == keyword_id for item in evidence)
                algorithm_count = connection.execute(sql.SQL(
                    "SELECT count(*) FROM {} WHERE keyword_id=%s AND status='COMPLETED'"
                ).format(sql.Identifier(schema, "algorithm_results")), (keyword_id,)).fetchone()[0]
                assert algorithm_count > 0
            fetch_job, _created = store.collection_job(fetch_id)
            assert len(fetch_job["ai_jobs"]) == 1
            assert set(fetch_job["ai_jobs"][0]["keyword_ids"]) == set(keyword_ids)

            first_proposal = store.get_proposal(fetched["proposal_ids"][0])
            accepted_ids = keyword_ids[:2]
            assert set(first_proposal["candidate_keyword_ids"]) == set(keyword_ids)
            assert store.patch_proposal(
                first_proposal["id"],
                name=ACCEPTED_COLLECTION_NAME,
                description="Reviewed synthetic collection for end-to-end acceptance.",
                support_ids=accepted_ids,
                allowed_sources={WIKIMEDIA_SOURCE_ID},
            )
            accepted_collection_id = store.resolve_proposal(
                first_proposal["id"], True,
                name=ACCEPTED_COLLECTION_NAME,
                description="Reviewed synthetic collection for end-to-end acceptance.",
                allowed_sources={WIKIMEDIA_SOURCE_ID},
            )
            assert accepted_collection_id
            assert {row["id"] for row in store.keywords_for(accepted_collection_id)} == set(accepted_ids)

            rejected_ids = keyword_ids[2:4]
            rejected_run = ai_service.classify_keyword_ids(
                store, rejected_ids, idempotency_key="ai08-rejected-review",
            )
            assert rejected_run["status"] == "COMPLETED"
            assert len(rejected_run["proposal_ids"]) == 1
            rejected_proposal_id = rejected_run["proposal_ids"][0]
            assert not store.resolve_proposal(
                rejected_proposal_id, False, allowed_sources={WIKIMEDIA_SOURCE_ID},
            )
            assert store.get_proposal(rejected_proposal_id)["status"] == "REJECTED"

            preview_ids = keyword_ids[4:]
            scope = {"keyword_ids": preview_ids, "collection_ids": [], "include_unassigned": False}
            preview = ai_service.classify_keyword_ids(
                store, preview_ids, idempotency_key="ai08-preview-apply", preview=True, scope=scope,
            )
            assert preview["mode"] == "PREVIEW"
            assert {item["keyword_id"] for item in preview["new_assignments"]} == set(preview_ids)
            calls_before_apply = provider.classification_calls
            applied = ai_service.apply_classification_preview(store, preview["job_id"])
            assert applied["mode"] == "APPLIED"
            assert applied["new_assignment_count"] == 2
            assert provider.classification_calls == calls_before_apply

            recommendation = ai_service.recommend(
                store, [preview_ids[0]],
                research_constraints="Explore a synthetic textile-accessory hypothesis.",
                idempotency_key="ai08-recommendation",
            )
            assert recommendation["status"] == "COMPLETED"
            assert recommendation["recommendations"][0]["label"] == "AI hypothesis"
            assert recommendation["lineage"]["data_profile"] == PUBLIC_DEMO
            assert provider.recommendation_calls == 1
            assert ai_service._quota()["daily_used"] == 4
            assert all(item.get("data_mode") == "SYNTHETIC"
                       for task, payload in provider.requests if task == "classification"
                       for item in payload["keywords"])
            assert "LIVE" not in repr(source)
        finally:
            connection.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                sql.Identifier(schema),
            ))
