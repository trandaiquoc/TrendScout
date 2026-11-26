"""Nanosecond timestamps and arbitrary document keys survive conversion."""

from datetime import datetime, timedelta, timezone

import pytest

from app.storage.analysis_document_codec import AnalysisTimestamp, decode_document, encode_document


def test_preserves_nanoseconds_nulls_and_nested_paths():
    stamp = AnalysisTimestamp(1969, 12, 31, 23, 59, 59,
                             nanosecond=999_999_999, tzinfo=timezone.utc)
    original = {"type": "timestamp", "v": None, "nested": [{"": stamp}]}
    payload, timestamps = encode_document(original)
    assert timestamps == [{"path": ["nested", 0, ""], "seconds": -1,
                           "nanoseconds": 999_999_999}]
    restored = decode_document(payload, timestamps)
    assert restored["nested"][0][""].nanosecond == 999_999_999
    assert restored["nested"][0][""] == stamp
    assert restored["v"] is None and restored["type"] == "timestamp"


def test_normalizes_timezone_without_losing_fraction():
    value = datetime(2026, 1, 1, 1, 2, 3, 456789, tzinfo=timezone(timedelta(hours=7)))
    payload, metadata = encode_document({"at": value})
    assert decode_document(payload, metadata)["at"] == value.astimezone(timezone.utc)


def test_rejects_naive_timestamp_and_corrupt_paths():
    with pytest.raises(ValueError, match="timezone"):
        encode_document({"at": datetime(2026, 1, 1)})
    with pytest.raises(ValueError):
        decode_document({"at": "not-null"}, [{"path": ["at"], "seconds": 0, "nanoseconds": 0}])
