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
COLLECTION_FILTER_VERSION = 'wikimedia-collection-quality-v0.1'
LOCALIZED_SPECIAL_NAMESPACES = frozenset({
    'speciális', 'xüsusi', 'özel', '特別', '特殊', '특수', 'विशेष', 'خاص',
    'სპეციალური', 'speciell', 'ειδικό', 'specialis', 'eri', 'posebno',
    'սպասարկող', 'พิเศษ', 'specjalna', 'מיוחד',
})
LOCALIZED_SEARCH_TITLES = frozenset({
    'search', 'sök', 'suchen', 'recherche', 'ricerca', 'pesquisar', 'buscar',
    'ara', 'axtar', 'haku', 'keresés', 'zoeken', 'zoek', '検索', '查找', '搜索',
    'खोज', 'بحث', 'искать', 'иск', 'chercher', 'cercar',
    'αναζήτηση', 'quaerere', 'otsimine', 'iskanje', 'որոնել', 'ค้นหา', 'szukaj',
    'חיפוש',
})
LOCALIZED_MAIN_PAGE_TITLES = frozenset({
    'pagina principale', 'hoofdpagina', 'príomhleathanach', 'forside',
    '위키백과:대문', 'wikipédia:página principal',
})
COLLECTION_PROJECT_NAMESPACES = frozenset({
    'wiktionary', 'wikisource', 'wikifunctions', 'wikibooks', 'wikimedia',
    'wikidata', 'commons', 'meta',
})


def technical_reason(project: str, title: str) -> str | None:
    if (project, title.casefold()) in LOCALIZED_MAIN_PAGES:
        return 'TECHNICAL_LOCALIZED_MAIN_PAGE'
    if title.casefold() in {'main page', '-'}:
        return 'TECHNICAL_MAIN_PAGE' if title != '-' else 'TECHNICAL_PLACEHOLDER'
    if ':' in title and title.split(':', 1)[0].strip().casefold() in TECHNICAL_NAMESPACES:
        return 'TECHNICAL_NAMESPACE'
    return None


def collection_technical_reason(project: str, title: str) -> str | None:
    """Apply legacy cleaning plus multilingual namespace aliases to Collection candidates."""
    existing_reason = technical_reason(project, title)
    if existing_reason:
        return existing_reason
    if title.casefold() in LOCALIZED_MAIN_PAGE_TITLES:
        return 'TECHNICAL_LOCALIZED_MAIN_PAGE'
    if ':' in title:
        namespace, page = (part.strip().casefold() for part in title.split(':', 1))
        if namespace in LOCALIZED_SPECIAL_NAMESPACES or page in LOCALIZED_SEARCH_TITLES:
            return 'TECHNICAL_NAMESPACE'
        if namespace in COLLECTION_PROJECT_NAMESPACES and page == 'main page':
            return 'TECHNICAL_MAIN_PAGE'
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
