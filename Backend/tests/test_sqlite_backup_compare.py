import json
import sqlite3
from pathlib import Path

import pytest

from app.collections.store import SCHEMA
from app.storage.sqlite_backup_compare import _normalize_schema_sql, compare_backups
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
