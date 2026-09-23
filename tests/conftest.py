import ipaddress
import socket
from collections.abc import Iterator
from typing import Any

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


class NetworkBlockedError(RuntimeError):
    """Raised when a test tries to reach the network. Use a fake or a cassette."""


@pytest.fixture(autouse=True)
def _block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests run offline: only loopback and Unix-socket connections are allowed."""
    real_connect = socket.socket.connect

    def guarded_connect(self: socket.socket, address: Any) -> None:
        if self.family in (socket.AF_INET, socket.AF_INET6) and not _is_loopback(address[0]):
            raise NetworkBlockedError(f"network access blocked in tests: {address!r}")
        real_connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False
