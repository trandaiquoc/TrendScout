"""Compatible Wikimedia cleaning entry point and legacy public imports."""
from app.processing.contracts import CleaningReport, CleaningResult, RejectedRecord
from app.processing.pipeline import normalize_title as _title, process_snapshot, schema_validation, scope_validation
from app.processing.contracts import ProcessingContext
from app.sources.wikimedia.cleaning_rules import LOCALIZED_MAIN_PAGES, TECHNICAL_NAMESPACES, technical_reason, validate_source


def _invalid(row, snapshot):
    context = ProcessingContext(snapshot)
    return schema_validation(row, context).reason or validate_source(row, context).reason


def clean_snapshot(snapshot, rows):
    rule = technical_reason if snapshot.source in {"wikimedia", "wikimedia_pageviews_top_per_country"} else lambda _project, _title: None
    validator = validate_source if snapshot.source in {"wikimedia", "wikimedia_pageviews_top_per_country"} else scope_validation
    return process_snapshot(snapshot, rows, source_rule=rule, validate_source=validator)
