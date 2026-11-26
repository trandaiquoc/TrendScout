"""Configuration handling never prints credentials and checks endpoint identity."""

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.storage.postgres_config import postgres_environment, verify_same_database
from app.storage.postgres_connection import PostgresConfigurationError


def test_env_file_reads_only_database_urls_and_environment_takes_precedence(tmp_path):
    path = tmp_path / ".env"
    path.write_text("SERPAPI_API_KEY=provider-secret\nDATABASE_URL='pooled-value'\n"
                    "DATABASE_URL_UNPOOLED=direct-value\n", encoding="utf-8")
    with patch("app.storage.postgres_config.verify_private_file"):
        values = postgres_environment({"DATABASE_URL": "override"}, path)
    assert values == {"DATABASE_URL": "override", "DATABASE_URL_UNPOOLED": "direct-value"}
    assert "provider-secret" not in repr(values)


def test_duplicate_environment_values_fail_without_disclosing_them(tmp_path):
    path = tmp_path / ".env"
    path.write_text("DATABASE_URL=secret\nDATABASE_URL=secret\n", encoding="utf-8")
    with patch("app.storage.postgres_config.verify_private_file"):
        with pytest.raises(PostgresConfigurationError) as captured:
            postgres_environment({}, path)
    assert "secret" not in str(captured.value)


def test_identity_compares_normalized_endpoint_cluster_and_database():
    identities = [
        ("sample-pooler.region.example", 123, "business"),
        ("sample.region.example", 123, "business"),
    ]

    @contextmanager
    def connect(_environment, *, direct):
        host, cluster, database = identities[int(direct)]
        yield SimpleNamespace(info=SimpleNamespace(host=host), execute=lambda _query:
                              SimpleNamespace(fetchone=lambda: (cluster, database)))

    with patch("app.storage.postgres_config.postgres_connection", connect):
        verify_same_database({})
        identities[1] = ("other.region.example", 123, "business")
        with pytest.raises(PostgresConfigurationError):
            verify_same_database({})
