"""Validate untrusted organic results without fetching their destination URLs."""

from dataclasses import replace
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from app.sources.serpapi.models import QueryContext, QueryEvidence, SearchRecord


def canonical_url(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise ValueError("Invalid result URL")
    if any(ord(c) < 33 or ord(c) == 127 for c in value):
        raise ValueError("Invalid result URL")
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        port = parsed.port
        if (parsed.scheme.lower() not in {"http", "https"} or not host
                or parsed.username is not None or parsed.password is not None):
            raise ValueError("Invalid result URL")
        host = host.encode("idna").decode("ascii").lower()
        if ":" in host:
            host = f"[{host}]"
        if port is not None and (parsed.scheme.lower(), port) not in {("http", 80), ("https", 443)}:
            host += f":{port}"
        pairs = parse_qsl(parsed.query, keep_blank_values=True, max_num_fields=100)
        sensitive = {"api_key", "apikey", "access_token", "token", "password", "authorization"}
        if any(key.lower() in sensitive for key, _ in pairs):
            raise ValueError("Sensitive URL parameter")
        pairs = [(k, v) for k, v in pairs if not k.lower().startswith("utm_")
                 and k.lower() not in {"gclid", "fbclid"}]
        return urlunsplit((parsed.scheme.lower(), host, parsed.path or "/", urlencode(pairs), ""))
    except (ValueError, UnicodeError):
        raise ValueError("Invalid result URL") from None


def _text(value: object, limit: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > limit or "\x00" in value:
        raise ValueError("Invalid result text")
    value.encode("utf-8", errors="strict")
    return value


def normalize_items(context: QueryContext, items: tuple[object, ...], *, fetched_at: datetime,
                    page: int, offset: int, http_status: int, cache_state: str,
                    provider_created_at: datetime | None
                    ) -> tuple[tuple[SearchRecord, ...], tuple[str, ...]]:
    if fetched_at.tzinfo is None or fetched_at.utcoffset() is None:
        raise ValueError("Fetch time must be timezone-aware")
    fetched_at = fetched_at.astimezone(timezone.utc)
    future_provider_time = provider_created_at is not None and provider_created_at > fetched_at
    if future_provider_time:
        provider_created_at = None
    records, rejected = [], []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            rejected.append("INVALID_ITEM")
            continue
        try:
            url = canonical_url(item.get("link"))
            title = _text(item.get("title"), 1000)
            snippet = _text(item.get("snippet"), 5000)
            position = item.get("position", index + 1)
            if type(position) is not int or not 1 <= position <= 10000:
                raise ValueError("Invalid result position")
        except (ValueError, UnicodeError):
            rejected.append("INVALID_RESULT")
            continue
        evidence = QueryEvidence(context, fetched_at, page, offset, position, http_status,
                                 cache_state, provider_created_at)
        flags = ["SYNTHETIC_OFFLINE_EVIDENCE", "QUERY_SCOPED_POSITION_NOT_SEARCH_VOLUME"]
        if future_provider_time:
            flags.append("PROVIDER_CREATION_TIME_IN_FUTURE")
        if provider_created_at is None:
            flags.append("PROVIDER_CREATION_TIME_UNAVAILABLE")
        if "position" not in item:
            flags.append("POSITION_INFERRED_FROM_PAGE_ORDER")
        if title is None:
            flags.append("MISSING_TITLE")
        if snippet is None:
            flags.append("MISSING_SNIPPET")
        records.append(SearchRecord(context.source_id, context.provider_id, url, title, snippet,
                                    fetched_at, (evidence,), tuple(flags)))
    return tuple(records), tuple(rejected)


def merge_record(existing: SearchRecord | None, incoming: SearchRecord) -> SearchRecord:
    if existing is None:
        return incoming
    if (existing.source_id, existing.canonical_url) != (incoming.source_id, incoming.canonical_url):
        raise ValueError("Cannot merge unrelated records")
    return replace(existing, evidence=existing.evidence + incoming.evidence,
                   quality_flags=tuple(dict.fromkeys((*existing.quality_flags, "DUPLICATE_URL"))))
