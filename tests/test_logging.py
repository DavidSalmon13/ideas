import json
from collections.abc import Iterator

import pytest
import structlog

from opp.observability.logging import bind_context, clear_context, configure_logging


@pytest.fixture(autouse=True)
def _reset_context() -> Iterator[None]:
    clear_context()
    yield
    clear_context()


def test_json_lines_carry_bound_context(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO", json=True)
    bind_context(run_id="run-1", stage="DISCOVERING", agent="discovery")

    structlog.get_logger().info("lead_found", count=3)

    line = json.loads(capsys.readouterr().out.strip())
    assert line["event"] == "lead_found"
    assert line["count"] == 3
    assert line["run_id"] == "run-1"
    assert line["stage"] == "DISCOVERING"
    assert line["agent"] == "discovery"
    assert line["level"] == "info"
    assert "timestamp" in line


def test_level_filtering(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("WARNING", json=True)

    structlog.get_logger().info("hidden")

    assert capsys.readouterr().out == ""


def test_secrets_are_redacted(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO", json=True)

    structlog.get_logger().info("provider_ready", api_key="sk-live-123", auth_token="abc")

    line = json.loads(capsys.readouterr().out.strip())
    assert line["api_key"] == "***"
    assert line["auth_token"] == "***"
