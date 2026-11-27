"""Full-detail and editability contracts for Collection proposal APIs."""

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.ai import router as ai_router
from app.api.collections import router as collections_router


class ProposalStore:
    def __init__(self):
        self.keyword_ids = [f"keyword-{index}" for index in range(8)]
        self.proposal = {
            "id": "proposal-1", "name": "Topic proposal", "description": "Description",
            "support_ids": list(self.keyword_ids), "candidate_keyword_ids": list(self.keyword_ids),
            "reason": "Related public keywords", "evidence_ids": ["evidence-1"],
            "source_ids": ["wikimedia_pageviews_top_per_country"], "job_id": "ai-job-1",
            "status": "PENDING",
        }
        self.patched = None

    def get_proposal(self, proposal_id):
        return dict(self.proposal) if proposal_id == self.proposal["id"] else None

    def keyword_records_by_ids(self, identifiers):
        return [{"id": identifier, "keyword": f"Topic {index}", "normalized": f"topic {index}",
                 "source": "wikimedia_pageviews_top_per_country", "project": "en.wikipedia",
                 "observed_at": "2026-11-26T00:00:00Z"}
                for index, identifier in enumerate(identifiers)]

    def patch_proposal(self, proposal_id, *, name=None, description=None, support_ids=None,
                       allowed_sources=None):
        self.patched = {
            "proposal_id": proposal_id, "name": name, "description": description,
            "support_ids": support_ids, "allowed_sources": allowed_sources,
        }
        return True

    def resolve_proposal(self, proposal_id, accept, **kwargs):
        if accept:
            self.proposal["status"] = "ACCEPTED"
            return "collection-1"
        return ""


def test_proposal_api_exposes_and_edits_every_keyword_and_returns_reclassification_scope():
    store = ProposalStore()
    service = SimpleNamespace(store=store)
    app = FastAPI()
    app.include_router(ai_router, prefix="/api/v1")
    app.include_router(collections_router, prefix="/api/v1")
    app.state.settings = SimpleNamespace(environment_profile="test")
    app.state.collection_service = service
    app.state.ai_service = SimpleNamespace(
        _allowed_sources={"wikimedia_pageviews_top_per_country"},
        config=SimpleNamespace(data_profile="PUBLIC_DATA"),
    )

    with TestClient(app) as client:
        detail = client.get("/api/v1/collections/suggestions/proposal-1")
        assert detail.status_code == 200
        body = detail.json()
        assert body["support_ids"] == store.keyword_ids
        assert [item["id"] for item in body["keywords"]] == store.keyword_ids
        assert all(item["available"] for item in body["keywords"])
        assert body["job_id"] == "ai-job-1"

        changed = client.patch("/api/v1/ai/proposals/proposal-1", json={
            "name": "Edited proposal", "description": "Reviewed description",
            "keyword_ids": store.keyword_ids,
        })
        assert changed.status_code == 200
        assert store.patched["support_ids"] == store.keyword_ids

        accepted = client.post("/api/v1/collections/suggestions/proposal-1/accept", json={})
        assert accepted.status_code == 200
        assert accepted.json()["collection_id"] == "collection-1"
        assert accepted.json()["reclassification_keyword_ids"] == store.keyword_ids
        assert accepted.json()["reclassification_candidate_count"] == len(store.keyword_ids)
