"""A structural report-quality rubric (plan.md §21's "report quality / plausibility",
tracked over time rather than a hard gate).

The spec suggests an LLM-judge scores the report against a checklist; offline we score the
same checklist deterministically from the persisted record — does the decision brief carry
the nine things a founder needs to decide (specs.md §18.1)? A real LLM-judge variant can
replace this later without changing callers. Returns the fraction of checks that pass."""

from opp.domain.opportunity import Opportunity
from opp.domain.report import Report

CHECKS = (
    "verdict",
    "thesis",
    "why_now",
    "economics_snapshot",
    "load_bearing_flagged",
    "next_experiment",
    "kill_risks",
    "small_team",
    "confidence",
)


def rubric_score(opp: Opportunity, report: Report) -> float:
    brief = report.brief
    passed = {
        "verdict": bool(brief.get("verdict")),
        "thesis": bool(brief.get("thesis")),
        "why_now": brief.get("why_now") is not None,
        "economics_snapshot": brief.get("base") is not None,
        "load_bearing_flagged": bool(opp.economics.load_bearing_assumption_ids),
        "next_experiment": brief.get("next_experiment") is not None,
        "kill_risks": bool(opp.red_team.attacks),
        "small_team": opp.small_team_feasibility.can_one_person_start is not None,
        "confidence": brief.get("evidence_quality_score") is not None,
    }
    return sum(passed.values()) / len(CHECKS)
