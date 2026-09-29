"""Run the AI-quality eval suite and print a scoreboard (plan.md §21).

    uv run python -m scripts.run_evals

Runs every golden offline through the funnel fakes and prints per-scenario metrics —
hallucination rate, triage kills, fatal-flaw detection, economics/small-team coverage,
report rubric and cost. Exits non-zero if the hallucination hard gate is breached, so it
can double as a CI check alongside ``pytest evals`` (which asserts the full set)."""

import sys

from evals.goldens import GOLDENS
from evals.harness import GoldenResult, run_golden


def _line(r: GoldenResult) -> str:
    verdict = r.verdict.value if r.verdict else "-"
    econ = "-" if r.economics_ok is None else ("ok" if r.economics_ok else "MISSING")
    team = "-" if r.small_team_ok is None else ("ok" if r.small_team_ok else "MISSING")
    rubric = f"{r.rubric_score:.2f}" if r.rubric_score is not None else "-"
    return (
        f"{r.golden.name:<20} {r.status.value:<18} {verdict:<24} "
        f"halluc={r.hallucinated_facts}/{r.fact_claims} "
        f"kills={r.anti_generic_kills} fatal={r.fatal_flaw!s:<5} "
        f"econ={econ:<8} team={team:<8} rubric={rubric:<5} ${r.cost_usd:.4f}"
    )


def main() -> int:
    results = [run_golden(golden) for golden in GOLDENS]
    print("AI-quality eval scoreboard\n" + "=" * 26)
    for result in results:
        print(_line(result))
        print(f"    tiers behind fact/estimate claims: {result.tier_distribution or '{}'}")

    hallucinated = sum(r.hallucinated_facts for r in results)
    print("\n" + ("HALLUCINATION GATE OK" if hallucinated == 0 else "HALLUCINATION GATE FAILED"))
    return 0 if hallucinated == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
