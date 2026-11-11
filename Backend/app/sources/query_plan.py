"""Deterministic, versioned demo query configuration; it performs no requests."""

import hashlib
import json
from dataclasses import dataclass

from app.sources.catalog import SOURCE_CAPABILITIES


@dataclass(frozen=True)
class QuerySeed:
    scope: str
    query: str


@dataclass(frozen=True)
class QueryPlan:
    plan_id: str
    version: int
    seed_provenance: str
    taxonomy_version: str
    taxonomy_scope: str
    market: str
    language: str
    time_filter_rule: str
    max_pages_per_query: int
    deduplication_version: str
    seeds: tuple[QuerySeed, ...]

    @property
    def query_request_budget_per_source(self) -> int:
        return len(self.seeds) * self.max_pages_per_query

    @property
    def fingerprint(self) -> str:
        payload = {
            "plan_id": self.plan_id,
            "version": self.version,
            "seed_provenance": self.seed_provenance,
            "taxonomy_version": self.taxonomy_version,
            "taxonomy_scope": self.taxonomy_scope,
            "queries": [{"scope": seed.scope, "query": seed.query} for seed in self.seeds],
            "provider_ids": [item.provider_id for item in SOURCE_CAPABILITIES],
            "market": self.market,
            "language": self.language,
            "time_filter_rule": self.time_filter_rule,
            "max_pages_per_query": self.max_pages_per_query,
            "deduplication_version": self.deduplication_version,
        }
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


DEFAULT_QUERY_PLAN = QueryPlan(
    plan_id="demo-us-en",
    version=1,
    seed_provenance="Curated demo seeds informed by TrendScout topics-v1.0 taxonomy",
    taxonomy_version="topics-v1.0",
    taxonomy_scope="All active topic parents plus open discovery",
    market="US",
    language="en-US",
    time_filter_rule="rolling_7_days_when_supported",
    max_pages_per_query=1,
    deduplication_version="entity-source-query-links-v1",
    seeds=(
        QuerySeed("fashion", "fashion clothing footwear accessories trends"),
        QuerySeed("home_living", "home living kitchen furniture decor trends"),
        QuerySeed("technology", "technology software gadgets electronics trends"),
        QuerySeed("gaming", "gaming video games hardware esports trends"),
        QuerySeed("beauty_personal_care", "beauty skincare cosmetics haircare trends"),
        QuerySeed("sports", "sports athletes teams competitions equipment trends"),
        QuerySeed("food_beverage", "food beverage cooking trends"),
        QuerySeed("health_fitness", "health fitness wellness trends"),
        QuerySeed("travel_lifestyle", "travel destinations lifestyle trends"),
        QuerySeed("business_finance", "business finance companies markets trends"),
        QuerySeed("news_politics", "news politics elections public policy current events"),
        QuerySeed("entertainment", "entertainment film music television anime trends"),
        QuerySeed("celebrity_public_figures", "creators celebrities public figures trends"),
        QuerySeed("open_discovery", "emerging topics new public conversations"),
    ),
)

_QUERY_PLANS = {(DEFAULT_QUERY_PLAN.plan_id, DEFAULT_QUERY_PLAN.version): DEFAULT_QUERY_PLAN}


def get_query_plan(plan_id: str, version: int) -> QueryPlan:
    try:
        return _QUERY_PLANS[(plan_id, version)]
    except KeyError:
        raise ValueError(f"Unsupported query plan: {plan_id} v{version}") from None
