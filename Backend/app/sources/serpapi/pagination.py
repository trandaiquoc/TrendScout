"""Plan validation and safe extraction of offsets from provider pagination metadata."""

import hashlib
import json
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

from app.sources.catalog import BING_SOURCE_ID, GOOGLE_SOURCE_ID, source_capability
from app.sources.query_plan import QueryPlan, get_query_plan
from app.sources.serpapi.models import QueryContext, ScopedOffset

_ENGINES = {GOOGLE_SOURCE_ID: "google", BING_SOURCE_ID: "bing"}


def build_contexts(plan: QueryPlan, source_id: str, started_at: datetime) -> tuple[QueryContext, ...]:
    if source_id not in _ENGINES:
        raise ValueError("Unsupported SERP source")
    if get_query_plan(plan.plan_id, plan.version) != plan:
        raise ValueError("Only the registered server-controlled query plan is supported")
    if started_at.tzinfo is None or started_at.utcoffset() is None:
        raise ValueError("Batch timestamp must be timezone-aware")
    capability = source_capability(source_id)
    contexts = []
    for seed in plan.seeds:
        value = {
            "source": source_id,
            "provider": capability.provider_id,
            "plan": plan.fingerprint,
            "query": seed.query,
            "scope": seed.scope,
            "market": "US",
            "language": "en-US",
        }
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
        contexts.append(QueryContext(
            source_id, capability.provider_id, plan.plan_id, plan.version, plan.fingerprint,
            hashlib.sha256(encoded).hexdigest(), seed.query, seed.scope,
            started_at.astimezone(timezone.utc),
        ))
    return tuple(contexts)


def validate_offset(context: QueryContext, cursor: ScopedOffset) -> int:
    if cursor.query_fingerprint != context.query_fingerprint or type(cursor.value) is not int:
        raise ValueError("Offset does not belong to this query")
    if context.source_id == GOOGLE_SOURCE_ID:
        valid = 0 <= cursor.value <= 990 and cursor.value % 10 == 0
    elif context.source_id == BING_SOURCE_ID:
        valid = 1 <= cursor.value <= 1000
    else:
        valid = False
    if not valid:
        raise ValueError("Invalid provider offset")
    return cursor.value


def initial_offset(context: QueryContext) -> ScopedOffset:
    if context.source_id not in _ENGINES:
        raise ValueError("Unsupported SERP source")
    return ScopedOffset(0 if context.source_id == GOOGLE_SOURCE_ID else 1,
                        context.query_fingerprint)


def validate_context(context: QueryContext) -> None:
    """Reject client-created queries and tampered plan or provider metadata."""
    try:
        plan = get_query_plan(context.plan_id, context.plan_version)
        valid = context in build_contexts(plan, context.source_id, context.started_at)
    except (AttributeError, TypeError, ValueError):
        valid = False
    if not valid:
        raise ValueError("Query is outside the registered plan")


def next_offset(context: QueryContext, current: ScopedOffset, link: object) -> ScopedOffset | None:
    """Extract only the next numeric offset; never request the supplied URL.

    Bing's documented examples have variable offsets; no fixed increment is assumed.
    """
    previous = validate_offset(context, current)
    if link is None:
        return None
    if not isinstance(link, str) or len(link) > 4096:
        raise ValueError("Invalid pagination link")
    try:
        parsed = urlsplit(link)
        if (parsed.scheme != "https" or parsed.hostname != "serpapi.com" or parsed.port
                or parsed.username or parsed.password or parsed.fragment
                or parsed.path not in {"/search", "/search.json"}):
            raise ValueError("Invalid pagination origin")
        values = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=30)
    except ValueError:
        raise ValueError("Invalid pagination link") from None
    parameter = "start" if context.source_id == GOOGLE_SOURCE_ID else "first"
    if (values.get("engine") != [_ENGINES[context.source_id]]
            or values.get("q") != [context.canonical_query]
            or len(values.get(parameter, [])) != 1):
        raise ValueError("Pagination link has wrong query scope")
    raw = values[parameter][0]
    if not raw.isascii() or not raw.isdigit() or len(raw) > 4:
        raise ValueError("Invalid pagination offset")
    cursor = ScopedOffset(int(raw), context.query_fingerprint)
    if validate_offset(context, cursor) <= previous:
        raise ValueError("Pagination did not advance")
    return cursor
