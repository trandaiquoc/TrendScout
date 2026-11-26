"""Collection membership and proposal parity on an isolated PostgreSQL schema."""

import os
import uuid
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from psycopg import sql
from psycopg.types.json import Jsonb
from starlette.requests import Request

from app.api.collections import BulkMembershipInput, bulk_membership
from app.collections.postgres_store import PostgresCollectionStore
from app.storage.postgres_connection import postgres_connection
from app.storage.postgres_migrations import apply_migrations


def test_postgres_membership_reconciliation_bulk_actions_and_proposals():
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
            store = PostgresCollectionStore(environment, schema=schema, data_mode_scope="LIVE")
            root = store.create({"name": "Membership test"})
            child = store.create({"name": "Nested", "parent_id": root})
            tables = {name: sql.Identifier(schema, name) for name in (
                "keywords", "memberships", "classification_results", "proposals", "suppression")}
            keyword_ids = ["kw-alpha", "kw-beta", "kw-gamma", "kw-ineligible"]
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.executemany(sql.SQL("""INSERT INTO {}(id,keyword,normalized,source,project,
                    observed_at,data_mode,evidence,context) VALUES (%s,%s,%s,'test','',%s,'LIVE',%s,'')""")
                    .format(tables["keywords"]), [
                        (key, key.removeprefix("kw-"), key.removeprefix("kw-"), "2026-11-25", Jsonb({}))
                        for key in keyword_ids
                    ])
                connection.execute(sql.SQL("""INSERT INTO {}(keyword_id,entity_type,entity_id,
                    entity_description,topic_category_ids,subcategory_ids,classification_status,
                    confidence_level,commercial_relevance,classification_method,classification_version,
                        taxonomy_version,rule_evidence,source_evidence,review_status,classified_at,
                        classification_reason) VALUES (%s,'OTHER',NULL,'','[]','[]','INELIGIBLE',
                    'LOW','NOT_ASSESSED','fixture','v1','v1','[]','{{}}','UNREVIEWED',now(),'fixture')""")
                    .format(tables["classification_results"]), ("kw-ineligible",))

            summary = store.workbench_summary()
            assert summary["stored_keywords"] == 4
            assert summary["eligible_keywords"] == 3
            assert summary["ineligible_topic_keywords"] == 1
            assert summary["sources"] == ["test"]
            assert store.collection_exists(root) is True
            assert store.collection_exists("missing") is False
            assert store.collection_keyword_record("kw-alpha")["evidence"] == {}
            assert store.classification_for("kw-ineligible")["topic_category_ids"] == []
            assert store.keyword_detail_metadata("kw-alpha")["decision"] is None
            assert store.collection_job("missing")[0] is None
            store.set_setting("creation_mode", "SUGGEST_ONLY")
            assert store.get_setting("creation_mode") == "SUGGEST_ONLY"

            def record(identifier, word=None):
                value = word or identifier.removeprefix("kw-")
                return {"id": identifier, "keyword": value, "normalized": value,
                        "source": "test", "project": "", "observed_at": "2026-11-25",
                        "data_mode": "LIVE", "evidence": {}, "context": ""}

            store.feedback_action(root, record("kw-alpha"), "ADD")
            listed = store.keywords_for(root)
            assert [item["id"] for item in listed] == ["kw-alpha"]
            assert listed[0]["origin"] == "USER" and listed[0]["topic_eligible"] is True
            store.feedback_action(root, record("kw-alpha", ""), "REMOVE")
            assert store.keywords_for(root) == []

            with connection.transaction():
                connection.execute(sql.SQL("""INSERT INTO {}(collection_id,keyword_id,origin,pinned,
                    score,reason,created_at) VALUES (%s,'kw-alpha','LOCAL_AI',0,0.7,'model',now()),
                    (%s,'kw-beta','LOCAL_AI',0,0.8,'model',now())""").format(tables["memberships"]),
                    (root, child))
            assert store.archive_unpinned_ai_memberships("reconcile-1", "test replay") == 2
            assert store.keywords_for(root) == []
            assert store.rollback_membership_reconciliation("reconcile-1") is True
            restored = {(item["id"], item["origin"]) for item in store.keywords_for(root)}
            assert restored == {("kw-alpha", "LOCAL_AI"), ("kw-beta", "LOCAL_AI")}
            assert store.rollback_membership_reconciliation("reconcile-1") is False
            nested = store.keywords_for(child)
            assert nested[0]["id"] == "kw-beta"

            added = store.bulk_membership_action(["kw-gamma", "missing"], "ADD", collection_id=root)
            assert added == {"updated": 1, "skipped": 1, "failed": 0}
            approved = store.bulk_membership_action(["kw-alpha"], "APPROVE", collection_id=root)
            assert approved["updated"] == 1
            excluded = store.bulk_membership_action(["kw-beta"], "EXCLUDE")
            assert excluded["updated"] == 1
            assert store.search_keywords(keyword_id="kw-beta")["items"][0]["eligible"] == 0
            restored_decision = store.bulk_membership_action(["kw-beta"], "RESTORE")
            assert restored_decision["updated"] == 1
            assert store.search_keywords(keyword_id="kw-beta")["items"][0]["eligible"] == 1
            store.feedback_action(child, record("kw-beta"), "ADD")
            moved = store.bulk_membership_action(["kw-gamma"], "MOVE", collection_id=child,
                                                 source_collection_id=root)
            assert moved["updated"] == 1
            direct_root = connection.execute(sql.SQL("SELECT keyword_id FROM {} WHERE collection_id=%s")
                                             .format(tables["memberships"]), (root,)).fetchall()
            direct_child = connection.execute(sql.SQL("SELECT keyword_id FROM {} WHERE collection_id=%s")
                                              .format(tables["memberships"]), (child,)).fetchall()
            assert {row[0] for row in direct_root} == {"kw-alpha"}
            assert {row[0] for row in direct_child} == {"kw-beta", "kw-gamma"}
            rejected = store.bulk_membership_action(["kw-beta"], "REJECT", collection_id=child)
            assert rejected["updated"] == 1

            with connection.cursor() as cursor:
                cursor.executemany(sql.SQL("""INSERT INTO {}(id,keyword,normalized,source,project,
                    observed_at,data_mode,evidence,context) VALUES (%s,%s,%s,'test','',%s,'LIVE',%s,'')""")
                    .format(tables["keywords"]), [
                        (f"kw-bulk-{index}", f"bulk-{index}", f"bulk-{index}",
                         "2026-11-25", Jsonb({})) for index in range(5)
                    ])
            filtered = store.bulk_membership_filtered(state="unassigned", action="ADD",
                collection_id=root, batch_size=2)
            assert filtered == {"updated": 6, "skipped": 0, "failed": 0}
            assert len(store.search_keywords(query="bulk-", state="manual")["items"]) == 5
            with connection.cursor() as cursor:
                cursor.executemany(sql.SQL("""INSERT INTO {}(id,keyword,normalized,source,project,
                    observed_at,data_mode,evidence,context) VALUES (%s,%s,%s,'test','',%s,'LIVE',%s,'')""")
                    .format(tables["keywords"]), [
                        (f"kw-api-bulk-{index}", f"api-bulk-{index}", f"api-bulk-{index}",
                         "2026-11-25", Jsonb({})) for index in range(3)
                    ])
            app = FastAPI()
            app.state.settings = SimpleNamespace(environment_profile="test")
            app.state.collection_service = SimpleNamespace(store=store)
            request = Request({"type": "http", "method": "POST", "scheme": "http",
                "server": ("127.0.0.1", 80), "client": ("127.0.0.1", 12345),
                "path": "/api/v1/collections/bulk", "query_string": b"",
                "headers": [(b"host", b"127.0.0.1")], "app": app})
            api_result = bulk_membership(BulkMembershipInput(action="ADD", all_filtered=True,
                query="api-bulk-", state="unassigned", collection_id=root), request)
            assert api_result == {"updated": 3, "skipped": 0, "failed": 0}

            with connection.transaction():
                connection.execute(sql.SQL("""INSERT INTO {}(id,name,description,support_ids,reason,
                    status,created_at,updated_at) VALUES ('proposal-1','Suggested','Description',%s,
                    'Evidence reason','PENDING',now(),now()),('proposal-2','Rejected','Description',%s,
                    'Evidence reason','PENDING',now(),now())""").format(tables["proposals"]),
                    (Jsonb(["kw-alpha", "kw-beta"]), Jsonb(["kw-gamma"])))
            assert {item["id"] for item in store.suggestions()} == {"proposal-1", "proposal-2"}
            accepted = store.resolve_proposal("proposal-1", True)
            assert accepted and {item["id"] for item in store.keywords_for(accepted)} == {"kw-alpha", "kw-beta"}
            assert store.resolve_proposal("proposal-1", False) is None
            assert store.resolve_proposal("proposal-2", False) == ""
            assert connection.execute(sql.SQL("SELECT count(*) FROM {} WHERE kind='REJECTED_PROPOSAL'")
                                      .format(tables["suppression"])).fetchone()[0] == 1
        finally:
            if created:
                connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
