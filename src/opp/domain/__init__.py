"""Pydantic domain models (Opportunity, Claim, Source, ...). Enums live in `opp.domain.enums`."""

from opp.domain.evaluation import DimensionScore, Evaluation
from opp.domain.evidence import Claim, Source
from opp.domain.lead import Kill, Lead
from opp.domain.opportunity import (
    Attack,
    BusinessModel,
    Competitor,
    Customer,
    EconomicMetrics,
    Economics,
    Experiment,
    Market,
    Opportunity,
    Problem,
    RedTeamAnalysis,
    Scenario,
    Scenarios,
    SmallTeamFeasibility,
)
from opp.domain.report import Report, Trace
from opp.domain.run import AgentRun, ResearchRun, RunConfig, Stage, WorkLedger

__all__ = [
    "AgentRun",
    "Attack",
    "BusinessModel",
    "Claim",
    "Competitor",
    "Customer",
    "DimensionScore",
    "EconomicMetrics",
    "Economics",
    "Evaluation",
    "Experiment",
    "Kill",
    "Lead",
    "Market",
    "Opportunity",
    "Problem",
    "RedTeamAnalysis",
    "Report",
    "ResearchRun",
    "RunConfig",
    "Scenario",
    "Scenarios",
    "SmallTeamFeasibility",
    "Source",
    "Stage",
    "Trace",
    "WorkLedger",
]
