import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from app.collections.store import SCHEMA
from app.storage.local_file_security import secure_private_directory, verify_private_file
from app.storage.sqlite_backup_compare import _normalize_schema_sql, compare_backups
from app.storage.sqlite_backup_review import write_reconciliation_review
from app.storage.sqlite_to_postgres import InvalidBackup


def _make_backup(path: Path, rows: list[tuple[str, str]]) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(SCHEMA)
        connection.execute("PRAGMA journal_mode=DELETE")
        for keyword_id, evidence in rows:
            connection.execute(
                """INSERT INTO keywords
                   (id, keyword, normalized, source, observed_at, data_mode, evidence)
                   VALUES (?, ?, ?, 'wikimedia', '2026-11-25', 'LIVE', ?)""",
                (keyword_id, "Example", "example", evidence),
            )


def test_compare_reports_aggregate_conflicts_without_row_data(tmp_path):
    local = tmp_path / "local.sqlite"
    production = tmp_path / "production.sqlite"
    _make_backup(local, [
        ("private-shared-id", '{"z":1.00,"flag":true}'),
        ("private-changed-id", '{"secret":"local-private-value"}'),
        ("private-local-only-id", "{\"state\":\"local-only\"}"),
    ])
    _make_backup(production, [
        ("private-shared-id", '{"flag":true,"z":1.0}'),
        ("private-changed-id", '{"secret":"production-private-value"}'),
        ("private-production-only-id", "{\"state\":\"production-only\"}"),
    ])

    report = compare_backups(local, production, batch_size=1)
    keywords = report["tables"]["keywords"]

    assert report["schema_compatible"] is True
    assert report["data_comparison_complete"] is True
    assert keywords["local_rows"] == 3
    assert keywords["production_rows"] == 3
    assert keywords["matched_rows"] == 2
    assert keywords["local_only_rows"] == 1
    assert keywords["production_only_rows"] == 1
    assert keywords["changed_rows"] == 1
    assert keywords["changed_cells_by_column"] == {"evidence": 1}
    assert keywords["json_representation_only_cells_by_column"] == {"evidence": 1}

    serialized = json.dumps(report)
    for private_text in (
        "private-shared-id", "private-changed-id", "private-local-only-id",
        "private-production-only-id", "local-private-value", "production-private-value",
    ):
        assert private_text not in serialized


def test_compare_reports_timestamp_order_without_assuming_timezone(tmp_path):
    local = tmp_path / "local.sqlite"
    production = tmp_path / "production.sqlite"
    cases = {
        "private-same": (
            "2026-11-25T12:00:00Z", "2026-11-25T12:00:00Z",
        ),
        "private-offset-equal": (
            "2026-11-25T14:00:00+02:00", "2026-11-25T12:00:00Z",
        ),
        "private-production-later": (
            "2026-11-25T00:00:00Z", "2026-11-26T00:00:00Z",
        ),
        "private-local-later": (
            "2026-11-26T00:00:00Z", "2026-11-25T00:00:00Z",
        ),
        "private-naive": ("2026-11-25", "2026-11-26"),
        "private-invalid": ("invalid-local-time", "invalid-production-time"),
    }
    rows = [(identifier, "{}") for identifier in cases]
    _make_backup(local, rows)
    _make_backup(production, rows)
    for path, index in ((local, 0), (production, 1)):
        with sqlite3.connect(path) as connection:
            connection.executemany(
                "UPDATE keywords SET observed_at=? WHERE id=?",
                [(timestamps[index], identifier) for identifier, timestamps in cases.items()],
            )

    report = compare_backups(local, production)
    timestamps = report["tables"]["keywords"]["timestamp_order_by_column"]["observed_at"]

    assert timestamps == {
        "shared_rows": 6,
        "local_aware": 4,
        "production_aware": 4,
        "local_naive": 1,
        "production_naive": 1,
        "local_invalid": 1,
        "production_invalid": 1,
        "not_ordered_pairs": 2,
        "equal": 2,
        "production_later": 1,
        "local_later": 1,
        "absolute_delta_days_min": 1.0,
        "absolute_delta_days_max": 1.0,
    }
    serialized = json.dumps(report)
    for private_text in (*cases, *(value for pair in cases.values() for value in pair)):
        assert private_text not in serialized


def test_compare_rejects_invalid_json_in_one_sided_row(tmp_path):
    local = tmp_path / "local.sqlite"
    production = tmp_path / "production.sqlite"
    _make_backup(local, [("private-row-id", "not-json")])
    _make_backup(production, [])

    with pytest.raises(InvalidBackup, match="invalid JSON in keywords.evidence"):
        compare_backups(local, production)


def test_compare_rejects_wal_sidecar_without_reading_database(tmp_path):
    local = tmp_path / "local.sqlite"
    production = tmp_path / "production.sqlite"
    _make_backup(local, [])
    _make_backup(production, [])
    Path(f"{local}-wal").write_bytes(b"not a WAL")

    with pytest.raises(InvalidBackup, match="WAL sidecar"):
        compare_backups(local, production)


def test_compare_reports_index_drift_by_column_without_index_names(tmp_path):
    local = tmp_path / "local.sqlite"
    production = tmp_path / "production.sqlite"
    _make_backup(local, [])
    _make_backup(production, [])
    with sqlite3.connect(production) as connection:
        connection.execute("CREATE INDEX keyword_source_probe ON keywords(source)")

    schema = compare_backups(local, production)["tables"]["keywords"]["schema"]

    assert schema["schema_compatible"] is False
    assert schema["data_layout_compatible"] is True
    assert schema["local_only_indexes"] == []
    assert schema["production_only_indexes"] == [{
        "unique": False,
        "origin": "c",
        "partial": False,
        "columns": ["source"],
    }]
    assert "keyword_source_probe" not in json.dumps(schema)


def test_schema_sql_normalization_ignores_layout_whitespace_only():
    local = "CREATE TABLE sample (id TEXT NOT NULL\n, payload TEXT DEFAULT 'a , b')"
    production = "CREATE TABLE sample(id TEXT NOT NULL, payload TEXT DEFAULT 'a , b')"

    assert _normalize_schema_sql(local) == _normalize_schema_sql(production)


def test_private_review_artifact_keeps_conflicts_unresolved_and_inputs_unchanged(tmp_path):
    secure_private_directory(tmp_path)
    local = tmp_path / "local.sqlite"
    production = tmp_path / "production.sqlite"
    output = tmp_path / "row-review.jsonl"
    _make_backup(local, [
        ("same-row", '{"a":1,"b":true}'),
        ("changed-row", '{"private":"local-value"}'),
        ("local-only-row", '{"source":"local"}'),
    ])
    _make_backup(production, [
        ("same-row", '{"b":true,"a":1.0}'),
        ("changed-row", '{"private":"production-value"}'),
        ("production-only-row", '{"source":"production"}'),
    ])
    before = (local.read_bytes(), production.read_bytes())

    result = write_reconciliation_review(local, production, output, batch_size=1)

    verify_private_file(output)
    assert (local.read_bytes(), production.read_bytes()) == before
    assert result["row_counts"] == {
        "changed": 1,
        "local_only": 1,
        "production_only": 1,
    }
    assert result["authority_decisions"] == 0

    lines = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    assert lines[0]["record_type"] == "manifest"
    assert lines[0]["authority_decision"] == "none"
    assert lines[-1]["record_type"] == "summary"
    rows = lines[1:-1]
    assert {(row["table"], row["status"]) for row in rows} == {
        ("keywords", "changed"),
        ("keywords", "local_only"),
        ("keywords", "production_only"),
    }

    changed = next(row for row in rows if row["status"] == "changed")
    assert changed["domain_hint"] == "provider_observations"
    assert changed["authority_decision"] == "unresolved"
    assert changed["changed_columns"] == ["evidence"]
    assert changed["timestamp_order"]["observed_at"]["relation"] == "not_orderable"
    assert changed["local_row"]["evidence"] == '{"private":"local-value"}'
    assert changed["production_row"]["evidence"] == '{"private":"production-value"}'
    assert "same-row" not in output.read_text(encoding="utf-8")


def test_private_review_artifact_never_overwrites_existing_destination(tmp_path):
    secure_private_directory(tmp_path)
    local = tmp_path / "local.sqlite"
    production = tmp_path / "production.sqlite"
    output = tmp_path / "row-review.jsonl"
    _make_backup(local, [])
    _make_backup(production, [])
    output.write_text("owner data", encoding="utf-8")

    with pytest.raises(FileExistsError, match="already exists"):
        write_reconciliation_review(local, production, output)

    assert output.read_text(encoding="utf-8") == "owner data"


def test_private_review_artifact_summarizes_binary_values_without_copying_them(tmp_path):
    secure_private_directory(tmp_path)
    local = tmp_path / "local.sqlite"
    production = tmp_path / "production.sqlite"
    output = tmp_path / "row-review.jsonl"
    _make_backup(local, [])
    _make_backup(production, [])
    binary_value = b"private-vector-bytes"
    with sqlite3.connect(local) as connection:
        connection.execute(
            """INSERT INTO cache
               (fingerprint, model_version, collection_version, text_hash, vector, dimension)
               VALUES ('local-cache-id', 'model-v1', 1, 'text-hash', ?, 3)""",
            (binary_value,),
        )

    write_reconciliation_review(local, production, output)

    lines = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    row = next(item for item in lines if item.get("table") == "cache")
    assert row["local_row"]["vector"] == {
        "$type": "binary",
        "sha256": hashlib.sha256(binary_value).hexdigest(),
        "size_bytes": len(binary_value),
    }
    assert binary_value.decode() not in output.read_text(encoding="utf-8")
