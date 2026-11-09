"""Auditable, conservative cleaning of one Wikimedia daily snapshot."""

from collections import Counter
from dataclasses import dataclass, replace
from datetime import date, datetime
import re
import unicodedata
from urllib.parse import unquote_to_bytes

from app.models.observations import DatasetSnapshot, FetchStatus, PageObservation

# Conservative known namespaces; unknown/localized names remain for later review.
TECHNICAL_NAMESPACES = frozenset({
    "special", "spezial", "especial", "spécial", "speciaal", "media", "mediawiki",
    "talk", "user", "user talk", "wikipedia", "wikipedia talk", "wikimedia",
    "wikidata", "file", "image", "file talk", "template", "template talk",
    "category", "category talk", "help", "help talk", "portal", "portal talk",
    "module", "module talk", "draft", "draft talk", "project", "project talk",
})

LOCALIZED_MAIN_PAGES = frozenset({
    ("ja.wikipedia", "メインページ"),
    ("fr.wikipedia", "wikipédia:accueil principal"),
})


@dataclass(frozen=True)
class RejectedRecord:
    reason: str
    observation: PageObservation


@dataclass(frozen=True)
class CleaningReport:
    observation_date: date
    fetch_status: FetchStatus
    raw_count: int | None
    accepted_count: int | None
    rejected_count: int | None
    duplicate_count: int | None
    rejection_reasons: dict[str, int]
    quality_flags: tuple[str, ...]


@dataclass(frozen=True)
class CleaningResult:
    report: CleaningReport
    accepted: tuple[PageObservation, ...]
    rejected: tuple[RejectedRecord, ...]
    duplicates: tuple[PageObservation, ...]
    adapter_rejected: tuple[object, ...] = ()


def _title(title: str) -> str:
    # MediaWiki page titles disallow percent-encoded byte sequences; decode once,
    # never decode repeatedly or treat '+' as a space. Preserve original separately.
    if re.search(r"%[0-9a-fA-F]{2}", title):
        title = unquote_to_bytes(title).decode("utf-8", errors="strict")
    return " ".join(unicodedata.normalize("NFC", title).replace("_", " ").split())


def _invalid(row: PageObservation, snapshot: DatasetSnapshot) -> str | None:
    if not all(isinstance(x, str) and x.strip() for x in (row.source, row.country_scope, row.access, row.project, row.article_original)):
        return "MISSING_REQUIRED_FIELD"
    if type(row.observation_date) is not date or row.observation_date != snapshot.observation_date:
        return "INVALID_OBSERVATION_DATE"
    if not isinstance(row.fetched_at, datetime) or row.fetched_at.tzinfo is None or row.fetched_at.utcoffset() is None:
        return "INVALID_FETCH_TIMESTAMP"
    if (row.source, row.country_scope, row.access) != (snapshot.source, snapshot.country_scope, snapshot.access):
        return "SNAPSHOT_SCOPE_MISMATCH"
    if row.views_ceil is None:
        return "MISSING_VIEWS"
    if type(row.views_ceil) is not int or row.views_ceil < 0:
        return "INVALID_VIEWS"
    if type(row.provider_rank) is not int or row.provider_rank < 1:
        return "INVALID_RANK"
    if not isinstance(row.raw_record, dict):
        return "MISSING_RAW_RECORD"
    return None


def clean_snapshot(snapshot: DatasetSnapshot, rows: list[PageObservation]) -> CleaningResult:
    if snapshot.item_count is None:
        if rows:
            raise ValueError("Snapshot without a source count cannot contain observations")
        report = CleaningReport(snapshot.observation_date, snapshot.fetch_status, None, None, None, None, {}, ("SOURCE_UNAVAILABLE",))
        return CleaningResult(report, (), (), ())
    if (type(snapshot.item_count) is not int or snapshot.item_count < 0
            or type(snapshot.rejected_count) is not int or snapshot.rejected_count < 0
            or snapshot.item_count != len(rows) + snapshot.rejected_count):
        raise ValueError("Adapter input counts do not reconcile")
    accepted = []
    rejected = []
    duplicates = []
    reasons: Counter[str] = Counter()
    if snapshot.rejected_count:
        reasons["ADAPTER_INVALID"] = snapshot.rejected_count
    seen = set()
    for row in rows:
        reason = _invalid(row, snapshot)
        title = ""
        if reason is None:
            try:
                title = _title(row.article_original)
            except UnicodeDecodeError:
                reason = "INVALID_TITLE_ENCODING"
        if reason is None and not title:
            reason = "EMPTY_TITLE"
        if reason is None and (row.project, title.casefold()) in LOCALIZED_MAIN_PAGES:
            reason = "TECHNICAL_LOCALIZED_MAIN_PAGE"
        if reason is None and title.casefold() in {"main page", "-"}:
            reason = "TECHNICAL_MAIN_PAGE" if title != "-" else "TECHNICAL_PLACEHOLDER"
        if reason is None and ":" in title:
            prefix = title.split(":", 1)[0].strip().casefold()
            if prefix in TECHNICAL_NAMESPACES:
                reason = "TECHNICAL_NAMESPACE"
        if reason:
            reasons[reason] += 1
            rejected.append(RejectedRecord(reason, row))
            continue
        key = (row.project, row.article_original, row.observation_date)
        if key in seen:
            duplicates.append(row)
            continue
        seen.add(key)
        accepted.append(replace(row, article_display=title, canonical_keyword=title))
    flags = []
    if snapshot.fetch_status != FetchStatus.SUCCESS:
        flags.append("SOURCE_PARTIAL_OR_ERROR")
    if snapshot.rejected_count and len(snapshot.rejected_records) != snapshot.rejected_count:
        flags.append("ADAPTER_REJECTIONS_WITHOUT_ROW_DETAILS")
    if duplicates:
        flags.append("DUPLICATES_REMOVED_FIRST_WINS")
    report = CleaningReport(snapshot.observation_date, snapshot.fetch_status, snapshot.item_count,
                            len(accepted), sum(reasons.values()), len(duplicates), dict(reasons), tuple(flags))
    return CleaningResult(report, tuple(accepted), tuple(rejected), tuple(duplicates), snapshot.rejected_records)
