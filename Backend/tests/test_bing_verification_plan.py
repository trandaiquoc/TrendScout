import hashlib
import unittest
from datetime import datetime, timezone

from app.sources.bing_search.verification_plan import build_verification_plan
from app.sources.catalog import BING_SOURCE_ID, GOOGLE_SOURCE_ID
from app.sources.google_trends.trending_now import normalize_keyword
from app.sources.serpapi.client import build_request_params
from app.sources.serpapi.pagination import initial_offset, validate_context

NOW = datetime(2026, 11, 16, 8, tzinfo=timezone.utc)


def row(keyword, *, category="Fashion", source=GOOGLE_SOURCE_ID, observed="2026-11-16T07:00:00Z"):
    return {
        "record_id": hashlib.sha256(keyword.casefold().encode()).hexdigest(),
        "canonical_keyword": keyword,
        "source_id": source,
        "data_mode": "LIVE",
        "project": "google_trends:US:en",
        "observed_at": observed,
        "fetched_at": "2026-11-16T07:05:00Z",
        "evidence": {"trend_clusters": [{
            "categories": [category], "active": True, "increase_percentage": 120,
        }]},
    }


class BingVerificationPlanTests(unittest.TestCase):
    def test_selects_one_saved_candidate_and_binds_bing_request_parameters(self):
        plan = build_verification_plan((row(" Pleated   tote "), row("other topic", category="Other")),
                                       policy_fingerprint="a" * 64, started_at=NOW)
        self.assertEqual(len(plan.contexts), 1)
        context = plan.contexts[0]
        self.assertEqual(context.source_id, BING_SOURCE_ID)
        self.assertEqual(plan.candidates[0].source_id, GOOGLE_SOURCE_ID)
        self.assertEqual(plan.candidates[0].reason, "PREFERRED_INDUSTRY_SIGNAL")
        self.assertIsNone(validate_context(context))
        self.assertEqual(build_request_params(context, initial_offset(context)), {
            "engine": "bing", "q": "Pleated tote", "mkt": "en-US",
            "first": 1, "output": "json",
        })

    def test_deduplicates_normalized_identities_and_skips_recent_or_non_google_rows(self):
        duplicate_a = row("Portable throw", category="Home & Living")
        duplicate_b = row(" portable   throw ", category="Home & Living")
        recent = row("Already checked")
        _, identity = normalize_keyword("Already checked")
        recent_digest = hashlib.sha256(identity.encode()).hexdigest()
        plan = build_verification_plan((duplicate_a, duplicate_b, recent,
                                       row("Ignore me", source=BING_SOURCE_ID)),
                                       policy_fingerprint="b" * 64, started_at=NOW,
                                       recently_verified=frozenset({recent_digest}))
        self.assertEqual(len(plan.candidates), 1)
        self.assertEqual(plan.candidates[0].keyword, "Portable throw")

    def test_rejects_tampered_query_and_naive_timestamps(self):
        plan = build_verification_plan((row("linen storage basket", category="Home"),),
                                       policy_fingerprint="c" * 64, started_at=NOW)
        context = plan.contexts[0]
        changed = context.__class__(**{**context.__dict__, "canonical_query": "arbitrary"})
        self.assertFalse(__import__(
            "app.sources.bing_search.verification_plan", fromlist=["validate_dynamic_context"]
        ).validate_dynamic_context(changed, policy_fingerprint="c" * 64))
        with self.assertRaises(ValueError):
            build_verification_plan((row("x"),), policy_fingerprint="d" * 64,
                                    started_at=datetime(2026, 11, 16))


if __name__ == "__main__":
    unittest.main()
