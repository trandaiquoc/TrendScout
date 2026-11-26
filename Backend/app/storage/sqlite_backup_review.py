"""Write a private, read-only row review for two Collections backups.

Unlike the aggregate comparator, this artifact includes the conflicting row
values. It is intended for owner-only review and never selects a winning
source or changes either backup.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
from collections import Counter
from contextlib import ExitStack, closing
from datetime import datetime, timezone
from pathlib import Path

from app.storage.local_file_security import verify_private_directory, verify_private_file
from app.storage.sqlite_backup_compare import (
    _JSON_COLUMNS,
    _ordered_rows,
    _table_metadata,
    _timestamp_value,
    compare_backups,
)
from app.storage.sqlite_to_postgres import TABLES, InvalidBackup, _check_source, _source_digest

_DOMAIN_HINTS = {
    "collections": "user_authored",
    "memberships": "user_authored",
    "feedback": "user_authored",
    "keyword_decisions": "user_authored",
    "research_records": "user_authored",
    "keywords": "provider_observations",
    "dataset_snapshots": "provider_observations",
    "entity_context": "provider_observations",
    "algorithm_observations": "provider_observations",
    "algorithm_results": "derived_results",
    "classification_results": "derived_results",
    "cache": "derived_cache",
    "fetch_jobs": "jobs_and_checkpoints",
    "membership_reconciliation_runs": "jobs_and_checkpoints",
    "membership_reconciliation_items": "jobs_and_checkpoints",
    "settings": "settings",
    "suppression": "decision_or_suppression_state",
    "proposals": "proposal_workflow",
}


def _review_value(value):
    if isinstance(value, bytes):
        return {
            "$type": "binary",
            "sha256": hashlib.sha256(value).hexdigest(),
            "size_bytes": len(value),
        }
    if isinstance(value, float) and not math.isfinite(value):
        raise InvalidBackup("source contains a non-finite numeric value")
    return value


def _row_values(row) -> dict:
    return {column: _review_value(row[column]) for column in row.keys()}


def _row_comparison(
    local_row,
    production_row,
    local_json: dict,
    production_json: dict,
    local_columns: set[str],
    production_columns: set[str],
    table: str,
) -> tuple[list[str], list[str]]:
    changed = []
    representation_only = []
    json_columns = _JSON_COLUMNS.get(table, frozenset())
    for column in sorted(local_columns | production_columns):
        if column not in local_columns or column not in production_columns:
            changed.append(column)
        elif column in json_columns:
            if local_json[column] != production_json[column]:
                changed.append(column)
            elif local_row[column] != production_row[column]:
                representation_only.append(column)
        elif local_row[column] != production_row[column]:
            changed.append(column)
    return changed, representation_only


def _timestamp_relations(local_row, production_row, shared_columns: set[str]) -> dict:
    relations = {}
    for column in sorted(name for name in shared_columns if name.endswith("_at")):
        local_time, local_state = _timestamp_value(local_row[column])
        production_time, production_state = _timestamp_value(production_row[column])
        if local_time is None or production_time is None:
            relation = "not_orderable"
        elif local_time > production_time:
            relation = "local_later"
        elif production_time > local_time:
            relation = "production_later"
        else:
            relation = "equal"
        relations[column] = {
            "local_timestamp_state": local_state,
            "production_timestamp_state": production_state,
            "relation": relation,
        }
    return relations


def _encode_line(output, record: dict) -> None:
    output.write(json.dumps(
        record,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ))
    output.write("\n")


def _open_immutable(path: Path):
    source = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)
    source.row_factory = sqlite3.Row
    return source


def write_reconciliation_review(
    local_backup: Path,
    production_backup: Path,
    output_path: Path,
    *,
    batch_size: int = 500,
) -> dict:
    """Write all changed and one-sided rows to a verified private JSONL file.

    Both inputs must be standalone owner-only backups. The destination parent
    must already be owner-only, and an existing destination is never replaced.
    Row payloads can contain private business/provider data, so the file is not
    suitable for stdout, a repository path, or a shared folder.
    """
    if type(batch_size) is not int or not 1 <= batch_size <= 5000:
        raise ValueError("batch size must be between 1 and 5000")

    local_path = Path(local_backup).absolute()
    production_path = Path(production_backup).absolute()
    target = Path(output_path).absolute()
    if target.name in ("", ".", ".."):
        raise ValueError("review output must name a file")
    verify_private_file(local_path)
    verify_private_file(production_path)
    verify_private_directory(target.parent)
    if target.exists() or target.is_symlink():
        raise FileExistsError("review output already exists")

    aggregate = compare_backups(local_path, production_path, batch_size=batch_size)
    if not aggregate["data_comparison_complete"]:
        raise InvalidBackup("row review requires comparable text primary keys in every table")
    if any(
        not table["schema"]["data_layout_compatible"]
        for table in aggregate["tables"].values()
    ):
        raise InvalidBackup("row review requires compatible table column and primary-key layouts")

    expected_digests = {
        "local": (aggregate["local_source"]["sha256"], aggregate["local_source"]["size_bytes"]),
        "production": (
            aggregate["production_source"]["sha256"],
            aggregate["production_source"]["size_bytes"],
        ),
    }
    if _source_digest(local_path) != expected_digests["local"]:
        raise InvalidBackup("local backup changed before row review")
    if _source_digest(production_path) != expected_digests["production"]:
        raise InvalidBackup("production backup changed before row review")

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(target, flags, 0o600)
    created = True
    try:
        verify_private_file(target)
        table_summaries = {}
        totals = Counter()
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
            descriptor = -1
            _encode_line(output, {
                "record_type": "manifest",
                "artifact_version": 1,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "sources": {
                    name: {"sha256": digest, "size_bytes": size}
                    for name, (digest, size) in expected_digests.items()
                },
                "schema_compatible": aggregate["schema_compatible"],
                "authority_decision": "none",
                "review_required": True,
                "notes": [
                    "Local and Production are caller-supplied labels, not authority decisions.",
                    "No rows were changed, imported, merged, or selected as winners.",
                    "Binary values are represented by SHA-256 and byte length, not copied.",
                ],
            })

            with ExitStack() as stack:
                local_source = stack.enter_context(closing(_open_immutable(local_path)))
                production_source = stack.enter_context(closing(_open_immutable(production_path)))
                _check_source(local_source)
                _check_source(production_source)

                for table in TABLES:
                    local_meta = _table_metadata(local_source, table)
                    production_meta = _table_metadata(production_source, table)
                    local_columns = set(local_meta["columns"])
                    production_columns = set(production_meta["columns"])
                    local_keys = [name for name, _ in local_meta["primary_key"]]
                    production_keys = [name for name, _ in production_meta["primary_key"]]
                    if local_keys != production_keys:
                        raise InvalidBackup("table primary-key layouts changed during review")

                    local_rows = iter(_ordered_rows(
                        local_source, table, local_meta["columns"], local_keys, batch_size
                    ))
                    production_rows = iter(_ordered_rows(
                        production_source, table, production_meta["columns"],
                        production_keys, batch_size
                    ))
                    left = next(local_rows, None)
                    right = next(production_rows, None)
                    counts = Counter()
                    changed_column_counts = Counter()

                    def emit(status, key, local_values=None, production_values=None,
                             changed_columns=(), representation_only=(), timestamps=None):
                        record = {
                            "record_type": "row_review",
                            "table": table,
                            "domain_hint": _DOMAIN_HINTS.get(table, "unclassified"),
                            "status": status,
                            "primary_key": dict(zip(local_keys, key, strict=True)),
                            "authority_decision": "unresolved",
                            "changed_columns": list(changed_columns),
                            "representation_only_columns": list(representation_only),
                            "timestamp_order": timestamps or {},
                            "local_row": local_values,
                            "production_row": production_values,
                        }
                        _encode_line(output, record)
                        counts[status] += 1
                        totals[status] += 1
                        changed_column_counts.update(changed_columns)

                    while left is not None or right is not None:
                        if right is None or (left is not None and left[0] < right[0]):
                            emit("local_only", left[0], _row_values(left[1]), None)
                            left = next(local_rows, None)
                            continue
                        if left is None or right[0] < left[0]:
                            emit("production_only", right[0], None, _row_values(right[1]))
                            right = next(production_rows, None)
                            continue

                        key, local_row, local_json = left
                        _, production_row, production_json = right
                        changed, representation_only = _row_comparison(
                            local_row,
                            production_row,
                            local_json,
                            production_json,
                            local_columns,
                            production_columns,
                            table,
                        )
                        if changed:
                            emit(
                                "changed",
                                key,
                                _row_values(local_row),
                                _row_values(production_row),
                                changed,
                                representation_only,
                                _timestamp_relations(
                                    local_row, production_row,
                                    local_columns & production_columns,
                                ),
                            )
                        left = next(local_rows, None)
                        right = next(production_rows, None)

                    table_summaries[table] = {
                        **dict(sorted(counts.items())),
                        "changed_column_counts": dict(sorted(changed_column_counts.items())),
                    }

            if _source_digest(local_path) != expected_digests["local"]:
                raise InvalidBackup("local backup changed during row review")
            if _source_digest(production_path) != expected_digests["production"]:
                raise InvalidBackup("production backup changed during row review")

            summary = {
                "record_type": "summary",
                "row_counts": dict(sorted(totals.items())),
                "tables": table_summaries,
                "authority_decisions": 0,
            }
            _encode_line(output, summary)
            output.flush()
            os.fsync(output.fileno())

        verify_private_file(target)
        return {
            "output_file": str(target),
            "row_counts": dict(sorted(totals.items())),
            "tables": table_summaries,
            "authority_decisions": 0,
        }
    except Exception:
        if descriptor >= 0:
            os.close(descriptor)
        if created:
            target.unlink(missing_ok=True)
        raise
