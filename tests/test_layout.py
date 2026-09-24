import importlib

import pytest

PACKAGES = [
    "opp.config",
    "opp.domain",
    "opp.db",
    "opp.evidence",
    "opp.research",
    "opp.llm",
    "opp.cassettes",
    "opp.agents",
    "opp.agents.specialists",
    "opp.orchestration",
    "opp.prompts",
    "opp.evaluation",
    "opp.reporting",
    "opp.api",
    "opp.cli",
    "opp.observability",
]


@pytest.mark.parametrize("name", PACKAGES)
def test_package_imports(name: str) -> None:
    importlib.import_module(name)
