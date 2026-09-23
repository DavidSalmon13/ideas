from collections.abc import Iterator

import pytest

from opp.config.settings import get_settings


@pytest.fixture(autouse=True)
def _isolated_settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[None]:
    """Keep tests off the developer's .env and real database."""
    db_path = tmp_path_factory.mktemp("db") / "test.db"
    monkeypatch.setenv("OPP_DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.chdir(tmp_path_factory.mktemp("cwd"))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
