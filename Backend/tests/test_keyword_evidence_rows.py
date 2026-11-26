import json

import pytest

from app.collections.store import CollectionStore


def test_keyword_evidence_rows_apply_exact_prefix_scope_and_limit(tmp_path):
    store = CollectionStore(tmp_path / "collections.sqlite", data_mode_scope="LIVE")
    rows = (
        ("live-match", "google_trends:daily", "LIVE", 1),
        ("live-ineligible", "google_trends:daily", "LIVE", 0),
        ("prefix-mismatch", "googleXtrends:daily", "LIVE", 1),
        ("other-mode", "google_trends:daily", "DEMO", 1),
        ("other-source", "google_trends:daily", "LIVE", 1),
    )
    with store.connect() as db:
        for identifier, project, mode, eligible in rows:
            source = "other" if identifier == "other-source" else "google_web_serp"
            db.execute("""INSERT INTO keywords(id,keyword,normalized,source,project,observed_at,
                data_mode,evidence,eligible) VALUES(?,?,?,?,?,?,?,?,?)""",
                (identifier, identifier, identifier, source, project, "2026-11-26", mode,
                 json.dumps({"candidate": identifier}), eligible))

    selected = store.keyword_evidence_rows(
        source_id="google_web_serp", project_prefix="google_trends:", data_mode="LIVE",
        eligible_only=True, limit=1,
    )
    assert [row["id"] for row in selected] == ["live-match"]
    assert json.loads(selected[0]["evidence"]) == {"candidate": "live-match"}

    with pytest.raises(ValueError, match="conflicts with store scope"):
        store.keyword_evidence_rows(source_id="google_web_serp", data_mode="DEMO")
    with pytest.raises(ValueError, match="between 1 and 5000"):
        store.keyword_evidence_rows(source_id="google_web_serp", limit=5001)
