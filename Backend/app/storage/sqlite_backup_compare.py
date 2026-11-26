"""Read-only, aggregate comparison of two standalone Collections backups.

The report deliberately excludes row keys and cell values. It identifies
overlaps and differences without choosing a source of truth or changing either
SQLite file.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter
from contextlib import ExitStack, closing
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from app.storage.sqlite_to_postgres import (
    TABLES,
    InvalidBackup,
    _bad_constant,
    _check_source,
    _identifier,
    _object_pairs,
    _source_digest,
)

_JSON_COLUMNS = {
    "collections": frozenset(("examples", "exclusions")),
    "keywords": frozenset(("evidence",)),
    "proposals": frozenset(("support_ids",)),
    "dataset_snapshots": frozenset(("source_manifest", "quality_flags")),
    "entity_context": frozenset(("occupations", "category_ids", "instance_types")),
    "classification_results": frozenset((
        "topic_category_ids", "subcategory_ids", "rule_evidence", "source_evidence",
    )),
}


def _quote(name: str) -> str:
    return '"' + _identifier(name).replace('"', '""') + '"'


def _normalize_schema_sql(statement: str | None) -> str | None:
    if statement is None:
        return None
    normalized = []
    in_single_quote = False
    in_double_quote = False
    pending_space = False
    index = 0
    while index < len(statement):
        char = statement[index]
        if in_single_quote:
            normalized.append(char)
            if char == "'":
                if index + 1 < len(statement) and statement[index + 1] == "'":
                    normalized.append("'")
                    index += 1
                else:
                    in_single_quote = False
        elif in_double_quote:
            normalized.append(char)
            if char == '"':
                if index + 1 < len(statement) and statement[index + 1] == '"':
                    normalized.append('"')
                    index += 1
                else:
                    in_double_quote = False
        elif char == "'":
            if pending_space and normalized and normalized[-1] not in "(,. ":
                normalized.append(" ")
            normalized.append(char)
            in_single_quote = True
            pending_space = False
        elif char == '"':
            if pending_space and normalized and normalized[-1] not in "(,. ":
                normalized.append(" ")
            normalized.append(char)
            in_double_quote = True
            pending_space = False
        elif char.isspace():
            pending_space = True
        else:
            if pending_space and normalized and char not in "(),." and normalized[-1] not in "(,. ":
                normalized.append(" ")
            normalized.append(char)
            pending_space = False
        index += 1
    return "".join(normalized)


def _canonical_json_value(value):
    if value is None:
        return ("null",)
    if type(value) is bool:
        return ("boolean", value)
    if isinstance(value, Decimal):
        if value == 0:
            return ("number", 0, (0,), 0)
        sign, digits, exponent = value.as_tuple()
        digits = list(digits)
        while digits and digits[-1] == 0:
            digits.pop()
            exponent += 1
        return ("number", sign, tuple(digits), exponent)
    if isinstance(value, str):
        return ("string", value)
    if isinstance(value, list):
        return ("array", tuple(_canonical_json_value(item) for item in value))
    if isinstance(value, dict):
        return (
            "object",
            tuple((key, _canonical_json_value(value[key])) for key in sorted(value)),
        )
    raise InvalidBackup("source contains an unsupported JSON value")


def _json_cell(value, table: str, column: str):
    if value is None:
        return ("sql-null",)
    if not isinstance(value, str):
        raise InvalidBackup(f"invalid JSON type in {table}.{column}")
    try:
        parsed = json.loads(
            value,
            object_pairs_hook=_object_pairs,
            parse_constant=_bad_constant,
            parse_float=Decimal,
            parse_int=Decimal,
        )
    except (TypeError, ValueError) as exc:
        raise InvalidBackup(f"invalid JSON in {table}.{column}") from exc
    return ("json", _canonical_json_value(parsed))


def _timestamp_value(value):
    """Parse only absolute timestamps; naive values cannot establish ordering."""
    if value is None:
        return None, "missing"
    if not isinstance(value, str):
        return None, "invalid"
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None, "invalid"
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None, "naive"
    return parsed.astimezone(timezone.utc), "aware"


def _table_metadata(source: sqlite3.Connection, table: str) -> dict:
    info = source.execute(f"PRAGMA table_info({_quote(table)})").fetchall()
    columns = [row["name"] for row in info]
    primary_key = [
        (row["name"], (row["type"] or "").strip().upper())
        for row in sorted((row for row in info if row["pk"]), key=lambda row: row["pk"])
    ]
    column_details = {
        row["name"]: {
            "type": " ".join((row["type"] or "").upper().split()),
            "not_null": bool(row["notnull"]),
            "default": row["dflt_value"],
            "pk_order": row["pk"],
        }
        for row in info
    }

    foreign_key_groups: dict[int, list[tuple]] = {}
    for row in source.execute(f"PRAGMA foreign_key_list({_quote(table)})"):
        foreign_key_groups.setdefault(row["id"], []).append((
            row["seq"], row["table"], row["from"], row["to"],
            row["on_update"], row["on_delete"], row["match"],
        ))
    foreign_keys = tuple(sorted(
        (tuple(sorted(group)) for group in foreign_key_groups.values()), key=repr
    ))

    indexes = []
    for row in source.execute(f"PRAGMA index_list({_quote(table)})"):
        index_name = row["name"]
        index_columns = tuple(
            (item["name"], bool(item["desc"]), item["coll"])
            for item in source.execute(f"PRAGMA index_xinfo({_quote(index_name)})")
            if item["key"]
        )
        indexes.append((bool(row["unique"]), row["origin"], bool(row["partial"]), index_columns))
    indexes = tuple(sorted(indexes, key=repr))

    definition = source.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    normalized_definition = _normalize_schema_sql(definition["sql"] if definition else None)
    definition_hash = (
        hashlib.sha256(normalized_definition.encode("utf-8")).hexdigest()
        if normalized_definition is not None else None
    )
    triggers = tuple(sorted(
        (row["name"], hashlib.sha256(row["sql"].encode("utf-8")).hexdigest())
        for row in source.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name=?", (table,)
        )
        if row["sql"] is not None
    ))
    table_options = next((
        (bool(row["wr"]), bool(row["strict"]))
        for row in source.execute("PRAGMA table_list")
        if row["schema"] == "main" and row["name"] == table
    ), None)

    return {
        "columns": columns,
        "primary_key": primary_key,
        "column_details": column_details,
        "foreign_keys": foreign_keys,
        "indexes": indexes,
        "definition_hash": definition_hash,
        "triggers": triggers,
        "table_options": table_options,
    }


def _schema_report(local: dict, production: dict) -> dict:
    local_columns = set(local["columns"])
    production_columns = set(production["columns"])
    shared_columns = local_columns & production_columns
    type_changes = sorted(
        name for name in shared_columns
        if local["column_details"][name]["type"] != production["column_details"][name]["type"]
    )
    nullability_changes = sorted(
        name for name in shared_columns
        if local["column_details"][name]["not_null"]
        != production["column_details"][name]["not_null"]
    )
    default_changes = sorted(
        name for name in shared_columns
        if local["column_details"][name]["default"]
        != production["column_details"][name]["default"]
    )
    primary_key_equal = local["primary_key"] == production["primary_key"]
    local_indexes = Counter(local["indexes"])
    production_indexes = Counter(production["indexes"])

    def describe_index(index: tuple) -> dict:
        unique, origin, partial, columns = index
        return {
            "unique": unique,
            "origin": origin,
            "partial": partial,
            "columns": [column for column, _, _ in columns],
        }

    def index_difference(left: Counter, right: Counter) -> list[dict]:
        different = []
        for signature in left:
            for _ in range(max(0, left[signature] - right[signature])):
                different.append(describe_index(signature))
        return sorted(different, key=lambda item: repr(item))

    layout_compatible = (
        local_columns == production_columns
        and all(
            local["column_details"][name]["type"]
            == production["column_details"][name]["type"]
            for name in local_columns
        )
        and primary_key_equal
        and bool(local["primary_key"])
        and all(key_type == "TEXT" for _, key_type in local["primary_key"])
    )
    fields_equal = {
        "column_types_equal": not type_changes,
        "nullability_equal": not nullability_changes,
        "defaults_equal": not default_changes,
        "primary_keys_equal": primary_key_equal,
        "foreign_keys_equal": local["foreign_keys"] == production["foreign_keys"],
        "indexes_equal": local["indexes"] == production["indexes"],
        "triggers_equal": local["triggers"] == production["triggers"],
        "table_options_equal": local["table_options"] == production["table_options"],
        "table_definitions_equal": local["definition_hash"] == production["definition_hash"],
    }
    exact = local_columns == production_columns and all(fields_equal.values())
    return {
        "schema_compatible": exact,
        "data_layout_compatible": layout_compatible,
        "column_order_equal": local["columns"] == production["columns"],
        "local_only_columns": sorted(local_columns - production_columns),
        "production_only_columns": sorted(production_columns - local_columns),
        "column_type_changes": type_changes,
        "nullability_changes": nullability_changes,
        "default_changes": default_changes,
        "local_primary_key": [name for name, _ in local["primary_key"]],
        "production_primary_key": [name for name, _ in production["primary_key"]],
        "local_only_indexes": index_difference(local_indexes, production_indexes),
        "production_only_indexes": index_difference(production_indexes, local_indexes),
        **fields_equal,
    }


def _row_key(row: sqlite3.Row, keys: list[str], table: str) -> tuple[str, ...]:
    values = tuple(row[key] for key in keys)
    if any(not isinstance(value, str) for value in values):
        raise InvalidBackup(f"{table} has a missing or non-text primary key")
    return values


def _ordered_rows(
    source: sqlite3.Connection,
    table: str,
    columns: list[str],
    keys: list[str],
    batch_size: int,
):
    projection = ", ".join(_quote(name) for name in columns)
    ordering = ", ".join(f"{_quote(name)} COLLATE BINARY" for name in keys)
    cursor = source.execute(
        f"SELECT {projection} FROM {_quote(table)} ORDER BY {ordering}"
    )
    json_columns = _JSON_COLUMNS.get(table, frozenset()) & set(columns)
    while rows := cursor.fetchmany(batch_size):
        for row in rows:
            key = _row_key(row, keys, table)
            json_values = {
                column: _json_cell(row[column], table, column)
                for column in json_columns
            }
            yield key, row, json_values


def _compare_table(
    local_source: sqlite3.Connection,
    production_source: sqlite3.Connection,
    table: str,
    batch_size: int,
) -> dict:
    local_meta = _table_metadata(local_source, table)
    production_meta = _table_metadata(production_source, table)
    schema = _schema_report(local_meta, production_meta)
    local_count = local_source.execute(f"SELECT count(*) FROM {_quote(table)}").fetchone()[0]
    production_count = production_source.execute(
        f"SELECT count(*) FROM {_quote(table)}"
    ).fetchone()[0]
    local_keys = [name for name, _ in local_meta["primary_key"]]
    production_keys = [name for name, _ in production_meta["primary_key"]]
    comparable = (
        bool(local_keys)
        and local_keys == production_keys
        and all(key_type == "TEXT" for _, key_type in local_meta["primary_key"])
        and all(key_type == "TEXT" for _, key_type in production_meta["primary_key"])
    )
    result = {
        "local_rows": local_count,
        "production_rows": production_count,
        "matched_rows": 0,
        "local_only_rows": 0,
        "production_only_rows": 0,
        "changed_rows": 0,
        "changed_cells_by_column": {},
        "json_representation_only_cells_by_column": {},
        "timestamp_order_by_column": {},
        "data_comparison_complete": comparable,
        "schema": schema,
    }
    if not comparable:
        return result

    local_columns = set(local_meta["columns"])
    production_columns = set(production_meta["columns"])
    shared_columns = sorted(local_columns & production_columns)
    json_shared = _JSON_COLUMNS.get(table, frozenset()) & set(shared_columns)
    local_rows = iter(_ordered_rows(
        local_source, table, local_meta["columns"], local_keys, batch_size
    ))
    production_rows = iter(_ordered_rows(
        production_source, table, production_meta["columns"], production_keys, batch_size
    ))
    local_row = next(local_rows, None)
    production_row = next(production_rows, None)
    changed_cells = Counter()
    representation_only = Counter()
    timestamp_columns = [name for name in shared_columns if name.endswith("_at")]
    timestamp_counts = {name: Counter() for name in timestamp_columns}
    timestamp_deltas = {name: [] for name in timestamp_columns}

    while local_row is not None and production_row is not None:
        local_key, local_values, local_json = local_row
        production_key, production_values, production_json = production_row
        if local_key < production_key:
            result["local_only_rows"] += 1
            local_row = next(local_rows, None)
        elif production_key < local_key:
            result["production_only_rows"] += 1
            production_row = next(production_rows, None)
        else:
            result["matched_rows"] += 1
            row_changed = False
            for column in shared_columns:
                left = local_values[column]
                right = production_values[column]
                if column in timestamp_counts:
                    local_time, local_kind = _timestamp_value(left)
                    production_time, production_kind = _timestamp_value(right)
                    counts = timestamp_counts[column]
                    counts["shared_rows"] += 1
                    counts[f"local_{local_kind}"] += 1
                    counts[f"production_{production_kind}"] += 1
                    if local_time is None or production_time is None:
                        counts["not_ordered_pairs"] += 1
                    elif production_time > local_time:
                        counts["production_later"] += 1
                        timestamp_deltas[column].append(
                            (production_time - local_time).total_seconds() / 86400
                        )
                    elif local_time > production_time:
                        counts["local_later"] += 1
                        timestamp_deltas[column].append(
                            (local_time - production_time).total_seconds() / 86400
                        )
                    else:
                        counts["equal"] += 1
                if column in json_shared:
                    if local_json[column] != production_json[column]:
                        changed_cells[column] += 1
                        row_changed = True
                    elif left != right:
                        representation_only[column] += 1
                elif left != right:
                    changed_cells[column] += 1
                    row_changed = True
            if row_changed:
                result["changed_rows"] += 1
            local_row = next(local_rows, None)
            production_row = next(production_rows, None)

    while local_row is not None:
        result["local_only_rows"] += 1
        local_row = next(local_rows, None)
    while production_row is not None:
        result["production_only_rows"] += 1
        production_row = next(production_rows, None)

    result["changed_cells_by_column"] = dict(sorted(changed_cells.items()))
    result["json_representation_only_cells_by_column"] = dict(sorted(representation_only.items()))
    result["timestamp_order_by_column"] = {
        column: {
            **dict(sorted(counts.items())),
            "absolute_delta_days_min": round(min(timestamp_deltas[column]), 6)
            if timestamp_deltas[column] else None,
            "absolute_delta_days_max": round(max(timestamp_deltas[column]), 6)
            if timestamp_deltas[column] else None,
        }
        for column, counts in timestamp_counts.items()
    }
    return result


def compare_backups(local_backup: Path, production_backup: Path, *, batch_size: int = 500) -> dict:
    """Compare standalone SQLite backups without printing row keys or values."""
    if type(batch_size) is not int or not 1 <= batch_size <= 5000:
        raise ValueError("batch size must be between 1 and 5000")
    local_path = Path(local_backup).absolute()
    production_path = Path(production_backup).absolute()
    local_digest, local_size = _source_digest(local_path)
    production_digest, production_size = _source_digest(production_path)
    initial_stats = {
        "local": (local_path.stat().st_size, local_path.stat().st_mtime_ns),
        "production": (production_path.stat().st_size, production_path.stat().st_mtime_ns),
    }

    with ExitStack() as stack:
        try:
            local_source = stack.enter_context(closing(sqlite3.connect(
                local_path.as_uri() + "?mode=ro&immutable=1", uri=True
            )))
            production_source = stack.enter_context(closing(sqlite3.connect(
                production_path.as_uri() + "?mode=ro&immutable=1", uri=True
            )))
            local_source.row_factory = sqlite3.Row
            production_source.row_factory = sqlite3.Row
            _check_source(local_source)
            _check_source(production_source)
            tables = {}
            for table in TABLES:
                tables[table] = _compare_table(
                    local_source, production_source, table, batch_size
                )
        except sqlite3.DatabaseError as exc:
            raise InvalidBackup("source is not a readable standalone SQLite backup") from exc

    if (local_path.stat().st_size, local_path.stat().st_mtime_ns) != initial_stats["local"]:
        raise InvalidBackup("local backup changed during comparison")
    if (production_path.stat().st_size, production_path.stat().st_mtime_ns) != initial_stats["production"]:
        raise InvalidBackup("production backup changed during comparison")
    if _source_digest(local_path) != (local_digest, local_size):
        raise InvalidBackup("local backup changed during comparison")
    if _source_digest(production_path) != (production_digest, production_size):
        raise InvalidBackup("production backup changed during comparison")

    totals = Counter()
    for table_result in tables.values():
        for field in (
            "local_rows", "production_rows", "matched_rows", "local_only_rows",
            "production_only_rows", "changed_rows",
        ):
            totals[field] += table_result[field]
    return {
        "local_source": {"sha256": local_digest, "size_bytes": local_size},
        "production_source": {"sha256": production_digest, "size_bytes": production_size},
        "schema_compatible": all(table["schema"]["schema_compatible"] for table in tables.values()),
        "data_comparison_complete": all(
            table["data_comparison_complete"] for table in tables.values()
        ),
        "totals": dict(totals),
        "tables": tables,
    }
