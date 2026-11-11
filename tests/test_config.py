from pathlib import Path

import pytest

from bom_agent.config import (
    DEFAULT_PARTDB_URL,
    DEFAULT_PUBLIC_URL,
    ConfigError,
    load_settings,
)


def test_defaults_when_env_empty():
    s = load_settings({})
    assert s.partdb_url == DEFAULT_PARTDB_URL
    assert s.has_partdb_token is False
    assert s.bom_db_path == Path("/data/bom.db")
    assert s.log_level == "INFO"


def test_env_overrides_and_trailing_slash_stripped():
    s = load_settings(
        {
            "PARTDB_URL": "https://partdb.example.com/",
            "PARTDB_API_TOKEN": "tcp_abc",
            "PARTDB_TIMEOUT": "2.5",
            "BOM_DB_PATH": "/tmp/bom.db",
            "LOG_LEVEL": "debug",
        }
    )
    assert s.partdb_url == "https://partdb.example.com"
    assert s.partdb_timeout == 2.5
    assert s.log_level == "DEBUG"
    assert s.has_partdb_token is True


def test_token_not_in_repr():
    s = load_settings(
        {"PARTDB_API_TOKEN": "tcp_sekret", "MCP_HTTP_TOKEN": "mcp_sekret"}
    )
    assert "tcp_sekret" not in repr(s)
    assert "mcp_sekret" not in repr(s)


def test_public_url_builds_the_intake_photo_address():
    """Ten adres otwiera przeglądarka przy części w Part-DB (#64), więc musi
    być publiczny — nie `localhost` procesu bom-agent."""
    s = load_settings({"PUBLIC_BASE_URL": "https://warsztat.example/"})
    assert s.intake_photo_url(17) == "https://warsztat.example/api/intake/17/photo"

    assert load_settings({}).public_base_url == DEFAULT_PUBLIC_URL


def test_blank_value_falls_back_to_default():
    s = load_settings({"PARTDB_URL": "   ", "LOG_LEVEL": ""})
    assert s.partdb_url == DEFAULT_PARTDB_URL
    assert s.log_level == "INFO"


@pytest.mark.parametrize(
    "env",
    [
        {"PARTDB_URL": "192.168.1.50:8090"},
        {"PARTDB_URL": "http://192.168.1.50:8090/api"},
        {"PUBLIC_BASE_URL": "intake.example.com"},
        {"LOG_LEVEL": "CHATTY"},
        {"PARTDB_TIMEOUT": "zero"},
        {"PARTDB_TIMEOUT": "-1"},
    ],
)
def test_rejects_nonsense(env):
    with pytest.raises(ConfigError):
        load_settings(env)
