"""Wikimedia v0.1 technical-page rules; deliberately conservative."""
from app.processing.contracts import StageResult

RULE_VERSION = 'wikimedia-technical-pages-v0.1'
TECHNICAL_NAMESPACES = frozenset({
    'special', 'spezial', 'especial', 'spécial', 'speciaal', 'media', 'mediawiki',
    'talk', 'user', 'user talk', 'wikipedia', 'wikipedia talk', 'wikimedia',
    'wikidata', 'file', 'image', 'file talk', 'template', 'template talk',
    'category', 'category talk', 'help', 'help talk', 'portal', 'portal talk',
    'module', 'module talk', 'draft', 'draft talk', 'project', 'project talk',
})
LOCALIZED_MAIN_PAGES = frozenset({
    ('ja.wikipedia', 'メインページ'),
    ('fr.wikipedia', 'wikipédia:accueil principal'),
})


def technical_reason(project: str, title: str) -> str | None:
    if (project, title.casefold()) in LOCALIZED_MAIN_PAGES:
        return 'TECHNICAL_LOCALIZED_MAIN_PAGE'
    if title.casefold() in {'main page', '-'}:
        return 'TECHNICAL_MAIN_PAGE' if title != '-' else 'TECHNICAL_PLACEHOLDER'
    if ':' in title and title.split(':', 1)[0].strip().casefold() in TECHNICAL_NAMESPACES:
        return 'TECHNICAL_NAMESPACE'
    return None


def validate_source(row, context):
    snapshot = context.snapshot
    if (row.source, row.country_scope, row.access) != (snapshot.source, snapshot.country_scope, snapshot.access):
        return StageResult(row, 'SNAPSHOT_SCOPE_MISMATCH')
    if row.views_ceil is None:
        return StageResult(row, 'MISSING_VIEWS')
    if type(row.views_ceil) is not int or row.views_ceil < 0:
        return StageResult(row, 'INVALID_VIEWS')
    if type(row.provider_rank) is not int or row.provider_rank < 1:
        return StageResult(row, 'INVALID_RANK')
    if not isinstance(row.raw_record, dict):
        return StageResult(row, 'MISSING_RAW_RECORD')
    return StageResult(row)
