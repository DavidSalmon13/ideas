import pytest
from pydantic import ValidationError

from opp.config.settings import Settings, get_settings


def test_defaults() -> None:
    settings = Settings(_env_file=None, database_url="sqlite:///./data/opp.db")

    assert settings.log_level == "INFO"
    assert settings.log_json is True


def test_reads_prefixed_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPP_LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("OPP_LOG_JSON", "false")

    settings = get_settings()

    assert settings.log_level == "DEBUG"
    assert settings.log_json is False


def test_rejects_unknown_log_level(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPP_LOG_LEVEL", "LOUD")

    with pytest.raises(ValidationError):
        Settings()
