from __future__ import annotations

from unittest.mock import patch

import pytest

from app.local_environment import LocalEnvironmentError, load_local_environment


def test_local_env_loads_all_config_keys_and_keeps_process_precedence(tmp_path):
    path = tmp_path / ".env"
    path.write_text(
        "# Dummy-only fixture\n"
        "DATABASE_URL=file-database\n"
        "OPENROUTER_API_KEY=dummy-openrouter-key\n"
        "SERPAPI_API_KEY='dummy-serpapi-key' # fixture\n"
        "export TREND_SCOUT_DEMO_PORT=8123\n"
        "GOOGLE_CLOUD_PROJECT=demo-project\n",
        encoding="utf-8",
    )
    environment = {
        "DATABASE_URL": "process-database",
        "OPENROUTER_API_KEY": "",
    }

    with patch("app.local_environment.verify_private_file"):
        loaded = load_local_environment(path, environment)

    assert environment == {
        "DATABASE_URL": "process-database",
        "OPENROUTER_API_KEY": "",
        "SERPAPI_API_KEY": "dummy-serpapi-key",
        "TREND_SCOUT_DEMO_PORT": "8123",
        "GOOGLE_CLOUD_PROJECT": "demo-project",
    }
    assert set(loaded) == {
        "SERPAPI_API_KEY", "TREND_SCOUT_DEMO_PORT", "GOOGLE_CLOUD_PROJECT",
    }
    assert "dummy" not in repr(loaded)


@pytest.mark.parametrize(
    "contents",
    [
        "OPENROUTER_API_KEY=dummy-one\nOPENROUTER_API_KEY=dummy-two\n",
        "OPENROUTER_API_KEY='unterminated\n",
        "not-an-assignment dummy-value\n",
    ],
)
def test_invalid_dotenv_fails_without_disclosing_values(tmp_path, contents):
    path = tmp_path / ".env"
    path.write_text(contents, encoding="utf-8")
    with patch("app.local_environment.verify_private_file"):
        with pytest.raises(LocalEnvironmentError) as captured:
            load_local_environment(path, {})
    assert "dummy" not in str(captured.value)


def test_missing_local_env_is_optional(tmp_path):
    assert load_local_environment(tmp_path / "missing.env", {}) == ()

