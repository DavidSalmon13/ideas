#!/usr/bin/env python
"""Drive a full research run headless, to a verdict (plan.md §28, Milestone M1).

A thin wrapper over the ``opp`` CLI that runs the whole funnel autonomously — discovery,
triage, shallow validation, the four deep-research specialists, synthesis, strategy,
red-team and judge — and prints the 1-page decision brief plus the pointer to the full
dossier. Every flag of ``opp discover`` is accepted (``--industry``, ``--geography``,
``--target-customer``, ``--signal``, ``--leads``, ``--budget``, ``--cassette`` ...).

    python scripts/run_research.py --industry HVAC --leads 30
    python scripts/run_research.py --cassette runs/hvac.json   # offline replay

Needs the schema in place first (``uv run alembic upgrade head``) and, for a live run,
``OPP_ANTHROPIC_API_KEY`` + ``OPP_TAVILY_API_KEY`` in the environment.
"""

import sys

from opp.cli.main import main


def run(argv: list[str] | None = None) -> int:
    """Run the funnel to completion, forwarding any ``opp discover`` flags."""
    return main(["discover", "--autonomous", *(argv if argv is not None else sys.argv[1:])])


if __name__ == "__main__":
    raise SystemExit(run())
