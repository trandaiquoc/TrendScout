"""Optional sidecar analysis; never mutates or persists legacy results."""
from collections import Counter
from dataclasses import dataclass
from time import perf_counter
import unicodedata

from app.processing.language_detection import VERSION as LANGUAGE_VERSION, LanguageResult, detect_language
from app.processing.similarity import VERSION as SIMILARITY_VERSION, candidates


@dataclass(frozen=True)
class AnalysisConfig:
    version: str = 'quality-sidecar-v0.1'
    language_enabled: bool = False
    similarity_enabled: bool = False
    noise_enabled: bool = False
    threshold: float = .8
    max_batch: int = 1000
    max_pairs: int = 10000
    max_per_keyword: int = 5
    max_posting: int = 50


@dataclass(frozen=True)
class QualityFlag:
    flag_id: str
    version: str
    severity: str
    reason: str
    evidence: str
    source: str
    observation_id: str


@dataclass(frozen=True)
class RecordAnalysis:
    record: object  # immutable KeywordResult, including raw source provenance
    language: object | None
    flags: tuple[QualityFlag, ...]


@dataclass(frozen=True)
class AdvancedReport:
    config_version: str
    cleaning_version: str
    raw_count: int | None
    accepted_count: int | None
    rejected_count: int | None
    exact_duplicate_count: int | None
    detected_languages: int | None
    unknown_languages: int | None
    language_distribution: dict[str, int]
    candidate_pair_count: int | None
    records_with_flags: int | None
    flag_count: int | None
    flags_by_id: dict[str, int]
    stage_status: dict[str, str]
    stage_reason: dict[str, str]
    comparisons: int | None
    detector_version: str
    similarity_version: str
    elapsed_seconds: float


@dataclass(frozen=True)
class QualityAnalysis:
    records: tuple[RecordAnalysis, ...]
    candidates: tuple
    report: AdvancedReport


def analyze(rows, cleaning_report, config=AnalysisConfig(), *, detector=detect_language):
    """Analyze accepted KeywordResults only. Unavailable input retains null counts."""
    start = perf_counter()
    rows = tuple(rows)
    if cleaning_report.accepted_count is None:
        if rows:
            raise ValueError('unavailable snapshot cannot contain records')
    elif len(rows) != cleaning_report.accepted_count:
        raise ValueError('accepted records do not reconcile with cleaning report')
    if cleaning_report.accepted_count is None:
        stages = {'language':'SKIPPED', 'similarity':'SKIPPED', 'noise':'SKIPPED'}
        report = AdvancedReport(config.version, cleaning_report.pipeline_version, cleaning_report.raw_count,
            None, cleaning_report.rejected_count, cleaning_report.duplicate_count, None, None, {}, None,
            None, None, {}, stages, {name:'source_unavailable' for name in stages}, None,
            LANGUAGE_VERSION, SIMILARITY_VERSION, perf_counter()-start)
        return QualityAnalysis((), (), report)
    states, reasons = {}, {}
    languages = []
    for row in rows:
        if not config.language_enabled:
            languages.append(None)
            continue
        try:
            detected = detector(row.canonical_keyword)
            if not isinstance(detected, LanguageResult):
                raise ValueError('invalid detector result')
            languages.append(detected)
        except Exception:
            # Do not log provider text or exception, and never modify the input.
            languages.append(None)
    states['language'] = ('SKIPPED' if not config.language_enabled else
                          'FAILED' if any(x is None for x in languages) else 'COMPLETED')
    reasons['language'] = 'disabled' if not config.language_enabled else 'detector_failed' if states['language'] == 'FAILED' else 'analyzed'
    if config.similarity_enabled:
        try:
            pairs, states['similarity'], reasons['similarity'], comparisons = candidates(
                rows, threshold=config.threshold, max_batch=config.max_batch, max_pairs=config.max_pairs,
                max_per_keyword=config.max_per_keyword, max_posting=config.max_posting)
        except Exception:
            pairs, comparisons = (), 0
            states['similarity'], reasons['similarity'] = 'FAILED', 'analysis_error'
    else:
        pairs, comparisons = (), 0
        states['similarity'], reasons['similarity'] = 'SKIPPED', 'disabled'
    paired = {r.observation_id for p in pairs for r in (p.left,p.right)}
    records = []
    for row, lang in zip(rows, languages):
        flags = []
        def add(flag_id, reason):
            flags.append(QualityFlag(flag_id, 'noise-rules-v0.1', 'info', reason,
                                     row.article_original, row.source, row.observation_id))
        if config.noise_enabled:
            title = row.canonical_keyword
            if len(title.strip()) < 4: add('SHORT_TITLE', 'title_under_four_characters')
            if not any(c.isalpha() for c in title): add('SYMBOLS_OR_NUMBERS_ONLY', 'no_letters')
            if lang is not None and lang.language == 'unknown': add('LANGUAGE_UNKNOWN', lang.reason)
            if row.observation_id in paired: add('NEAR_DUPLICATE_CANDIDATE', 'similarity_requires_review')
            if not row.source or not row.project or not row.country_scope or not row.access: add('MISSING_SOURCE_METADATA', 'missing_scope_or_project')
            if any(unicodedata.category(c) in ('Cc','Cf') for c in title): add('UNUSUAL_UNICODE', 'control_or_format_character')
            if title.casefold() in {'wikifunctions:main page','wiktionary:main page','wikisource:main page'}: add('POSSIBLE_TECHNICAL_PAGE', 'known_unreviewed_title')
        records.append(RecordAnalysis(row, lang, tuple(flags)))
    states['noise'] = 'COMPLETED' if config.noise_enabled else 'SKIPPED'
    reasons['noise'] = 'analyzed' if config.noise_enabled else 'disabled'
    distribution = Counter(x.language for x in languages if x is not None)
    flag_counts = Counter(f.flag_id for r in records for f in r.flags)
    report = AdvancedReport(config.version, cleaning_report.pipeline_version, cleaning_report.raw_count,
        cleaning_report.accepted_count, cleaning_report.rejected_count, cleaning_report.duplicate_count,
        sum(n for k,n in distribution.items() if k != 'unknown'), distribution.get('unknown', 0), dict(sorted(distribution.items())),
        len(pairs), sum(bool(r.flags) for r in records), sum(flag_counts.values()), dict(sorted(flag_counts.items())),
        states, reasons, comparisons, LANGUAGE_VERSION, SIMILARITY_VERSION, perf_counter()-start)
    return QualityAnalysis(tuple(records), pairs, report)
