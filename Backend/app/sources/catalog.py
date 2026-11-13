"""Versioned provider endpoints and capabilities; adapters remain source-specific."""

from dataclasses import dataclass

WIKIMEDIA_SOURCE_ID = "wikimedia_pageviews_top_per_country"
YOUTUBE_SOURCE_ID = "youtube_video_search"
GOOGLE_SOURCE_ID = "google_web_serp"
BING_SOURCE_ID = "bing_web_serp"

DEFAULT_WIKIMEDIA_USER_AGENT = "TrendScout/0.1 (https://github.com/trandaiquoc/TrendScout)"


@dataclass(frozen=True)
class SourceCapability:
    source_id: str
    provider_id: str
    capability_version: int
    endpoint: str
    method: str
    readiness: str
    default_enabled: bool
    adapter_status: str
    approval_status: str
    configuration_status: str
    live_verification_status: str
    supported_data_modes: tuple[str, ...]
    known_limitations: tuple[str, ...]
    credential_reference: str | None
    market: str
    language: str | None
    query_based: bool
    metric_name: str
    metric_unit: str
    metric_description: str
    capabilities: tuple[str, ...]


SOURCE_CAPABILITIES = (
    SourceCapability(
        source_id=WIKIMEDIA_SOURCE_ID,
        provider_id="wikimedia_aqs",
        capability_version=1,
        endpoint=(
            "https://wikimedia.org/api/rest_v1/metrics/pageviews/top-per-country/US/all-access"
        ),
        method="GET",
        readiness="IMPLEMENTED",
        default_enabled=True,
        adapter_status="IMPLEMENTED",
        approval_status="PENDING_LIVE_APPROVAL",
        configuration_status="NOT_REQUIRED",
        live_verification_status="UNVERIFIED",
        supported_data_modes=("SYNTHETIC", "LIVE"),
        known_limitations=(
            "Top-list membership is censored; absence is not zero.",
            "Approximate pageviews do not measure unique audience or purchase intent.",
            "Live connectivity has not been verified.",
        ),
        credential_reference=None,
        market="US country-scoped Wikimedia top list",
        language=None,
        query_based=False,
        metric_name="views_ceil",
        metric_unit="approx_pageviews_upper_bound",
        metric_description="Approximate upper-bound pageviews for listed pages",
        capabilities=(
            "daily_snapshot",
            "latest_available",
            "seven_day_history",
            "top_list_censored",
        ),
    ),
    SourceCapability(
        source_id=YOUTUBE_SOURCE_ID,
        provider_id="youtube_data_api_v3",
        capability_version=1,
        endpoint="https://www.googleapis.com/youtube/v3/search",
        method="GET",
        readiness="PENDING_APPROVAL",
        default_enabled=False,
        adapter_status="IMPLEMENTED_OFFLINE_VERIFIED",
        approval_status="PENDING_APPROVAL",
        configuration_status="NOT_CONFIGURED",
        live_verification_status="NOT_VERIFIED",
        supported_data_modes=("SYNTHETIC",),
        known_limitations=(
            "Offline fixture adapter only; live API execution is not configured or enabled.",
            "Search rank is not view count, search volume, trending rank, or product demand.",
            "YouTube usage, retention, and analytics policy approval is pending.",
        ),
        credential_reference="YOUTUBE_API_KEY",
        market="US regionCode filter; not author or viewer residence",
        language="en relevance filter",
        query_based=True,
        metric_name="result_rank",
        metric_unit="ordinal result position",
        metric_description="Position in query-scoped YouTube search results",
        capabilities=("query_search", "next_page_token", "region_filter", "language_filter"),
    ),
    SourceCapability(
        source_id=GOOGLE_SOURCE_ID,
        provider_id="serpapi_google",
        capability_version=1,
        endpoint="https://serpapi.com/search.json",
        method="GET",
        readiness="PENDING_APPROVAL",
        default_enabled=False,
        adapter_status="IMPLEMENTED_OFFLINE_VERIFIED",
        approval_status="PENDING_APPROVAL",
        configuration_status="NOT_CONFIGURED",
        live_verification_status="NOT_VERIFIED",
        supported_data_modes=("SYNTHETIC",),
        known_limitations=(
            "Offline mock transport only; live execution is not configured or enabled.",
            "SERP position is not search volume or purchase demand.",
            "Provider, terms, retention, and cost require approval.",
        ),
        credential_reference="SERPAPI_API_KEY",
        market="Google gl=us localization filter; not author or viewer residence",
        language="Google hl=en interface/results filter",
        query_based=True,
        metric_name="result_rank",
        metric_unit="ordinal result position",
        metric_description="Position in the returned Google SERP sample",
        capabilities=("query_search", "offset_pagination", "provider_sample"),
    ),
    SourceCapability(
        source_id=BING_SOURCE_ID,
        provider_id="serpapi_bing",
        capability_version=1,
        endpoint="https://serpapi.com/search.json",
        method="GET",
        readiness="PENDING_APPROVAL",
        default_enabled=False,
        adapter_status="IMPLEMENTED_OFFLINE_VERIFIED",
        approval_status="PENDING_APPROVAL",
        configuration_status="NOT_CONFIGURED",
        live_verification_status="NOT_VERIFIED",
        supported_data_modes=("SYNTHETIC",),
        known_limitations=(
            "Offline mock transport only; live execution is not configured or enabled.",
            "SERP position is not search volume or purchase demand.",
            "Provider, terms, retention, and cost require approval.",
        ),
        credential_reference="SERPAPI_API_KEY",
        market="Bing mkt=en-US localization filter; not author or viewer residence",
        language="en-US localization filter",
        query_based=True,
        metric_name="result_rank",
        metric_unit="ordinal result position",
        metric_description="Position in the returned Bing SERP sample",
        capabilities=("query_search", "offset_pagination", "provider_sample"),
    ),
)

_BY_SOURCE_ID = {item.source_id: item for item in SOURCE_CAPABILITIES}


def source_capability(source_id: str) -> SourceCapability:
    """Return a public capability without implying an adapter is available."""
    try:
        return _BY_SOURCE_ID[source_id]
    except KeyError:
        raise ValueError(f"Unsupported source capability: {source_id}") from None
