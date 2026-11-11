"""Versioned query plan identity must be deterministic and explicit."""

import unittest

from app.sources.query_plan import DEFAULT_QUERY_PLAN, get_query_plan


class QueryPlanTests(unittest.TestCase):
    def test_default_plan_contains_all_topic_seeds_and_open_discovery(self):
        plan = DEFAULT_QUERY_PLAN

        self.assertEqual((plan.plan_id, plan.version), ("demo-us-en", 1))
        self.assertEqual((plan.market, plan.language), ("US", "en-US"))
        self.assertEqual(plan.taxonomy_version, "topics-v1.0")
        self.assertEqual(len(plan.seeds), 14)
        self.assertEqual(len({seed.query for seed in plan.seeds}), 14)
        self.assertEqual(plan.seeds[0].scope, "fashion")
        self.assertIn("home_living", {seed.scope for seed in plan.seeds})
        self.assertEqual(plan.seeds[-1].scope, "open_discovery")
        self.assertEqual(plan.max_pages_per_query, 1)

    def test_fingerprint_is_stable_and_plan_lookup_is_versioned(self):
        first = get_query_plan("demo-us-en", 1)
        second = get_query_plan("demo-us-en", 1)

        self.assertEqual(first.fingerprint, second.fingerprint)
        self.assertEqual(len(first.fingerprint), 64)
        self.assertEqual(
            first.fingerprint, "57f80bbd40ba13bed596f8f7e5fb30787f9cf5ea854cef32d9a4d5cf5713f5ac"
        )
        with self.assertRaisesRegex(ValueError, "Unsupported query plan"):
            get_query_plan("demo-us-en", 2)


if __name__ == "__main__":
    unittest.main()
