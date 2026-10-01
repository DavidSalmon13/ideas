"""Enumerations from the domain vocabulary (specs.md §8-§9, §17, §19; intent.md §11-§16)."""

from enum import StrEnum


class Level(StrEnum):
    """Used for claim confidence and source credibility tier."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class ClaimType(StrEnum):
    FACT = "fact"
    ESTIMATE = "estimate"
    ASSUMPTION = "assumption"
    # "Looked, could not determine." A first-class answer (intent.md §4, specs.md §14).
    UNKNOWN = "unknown"


class ValueStatus(StrEnum):
    """The verified|estimated|assumed|unknown tag used for economics values (specs.md §10, §14)."""

    VERIFIED = "verified"
    ESTIMATED = "estimated"
    ASSUMED = "assumed"
    UNKNOWN = "unknown"


class EvidenceStrength(StrEnum):
    STRONG = "strong"
    MODERATE = "moderate"
    WEAK = "weak"


class Stance(StrEnum):
    SUPPORTING = "supporting"
    OPPOSING = "opposing"
    NEUTRAL = "neutral"


class SourceType(StrEnum):
    REGULATORY = "regulatory"
    INDUSTRY_REPORT = "industry_report"
    TRADE_PRESS = "trade_press"
    REVIEW = "review"
    FORUM = "forum"
    VENDOR = "vendor"
    PRICING_PAGE = "pricing_page"
    JOB_POST = "job_post"
    OTHER = "other"


class BusinessShape(StrEnum):
    SAAS = "saas"
    SERVICE = "service"
    MARKETPLACE = "marketplace"
    DATA = "data"
    LICENSING = "licensing"
    INFOPRODUCT = "infoproduct"
    ECOMMERCE = "ecommerce"
    PHYSICAL = "physical"
    ROLLUP = "rollup"
    ARBITRAGE = "arbitrage"
    INFRA = "infra"
    OTHER = "other"


class SignalSource(StrEnum):
    """Discovery strategies (specs.md §4.2). The first five mine B2B/SMB automation pain;
    the rest mine consumer-app opportunities (gaming, social, LLM-native products) that a
    solo builder would ship as their own product rather than sell to other businesses."""

    UNMET_NEED = "unmet_need"
    INCUMBENT_WEAKNESS = "incumbent_weakness"
    REGULATORY_TECH_CHANGE = "regulatory_tech_change"
    MANUAL_PROCESS = "manual_process"
    MARKET_INEFFICIENCY = "market_inefficiency"
    COMMUNITY_OBSESSION = "community_obsession"
    APP_STORE_GAP = "app_store_gap"
    LLM_CAPABILITY_UNLOCK = "llm_capability_unlock"
    CREATOR_TOOLING_GAP = "creator_tooling_gap"
    VIRAL_MECHANIC_MINING = "viral_mechanic_mining"


class LeadStatus(StrEnum):
    NEW = "new"
    KEPT = "kept"
    KILLED = "killed"
    DUPLICATE = "duplicate"


class OpportunityStatus(StrEnum):
    NEW = "new"
    PROMOTED = "promoted"
    PURSUED = "pursued"
    PARKED = "parked"
    DISCARDED = "discarded"


class KillSubject(StrEnum):
    LEAD = "lead"
    OPPORTUNITY = "opportunity"


class FailedGate(StrEnum):
    """Why something was killed: specificity tests, profile hard gates, red-team, dedup."""

    NAMED_CUSTOMER = "named_customer"
    SPECIFIC_PAIN = "specific_pain"
    WHY_NOW = "why_now"
    CAPITAL = "capital"
    TIME_TO_REVENUE = "time_to_revenue"
    FATAL_FLAW = "fatal_flaw"
    DUPLICATE = "duplicate"
    # A generic-pattern lead ("AI chatbot for X") without the stronger evidence it needs.
    GENERIC_PATTERN = "generic_pattern"


class DedupStatus(StrEnum):
    """State of an entry in the cross-run dedup index (plan.md §5, §14)."""

    ACTIVE = "active"
    DISCARDED = "discarded"
    KILLED = "killed"


class CompetitorType(StrEnum):
    DIRECT = "direct"
    INDIRECT = "indirect"
    SUBSTITUTE = "substitute"


class ExperimentMethod(StrEnum):
    INTERVIEW = "interview"
    LANDING_PAGE = "landing_page"
    PRESALE = "presale"
    OUTREACH = "outreach"
    MVP = "mvp"
    DESK_RESEARCH = "desk_research"


class Severity(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class EvidenceBasis(StrEnum):
    """What a provisional dimension score rests on (specs.md §5.3)."""

    SOURCED = "sourced"
    INFERRED = "inferred"
    INSUFFICIENT = "insufficient"


class Dimension(StrEnum):
    """Evaluation dimensions, each scored 1-5 (specs.md §17.2)."""

    REVENUE_POTENTIAL = "revenue_potential"
    GROSS_MARGIN = "gross_margin"
    TIME_TO_FIRST_REVENUE = "time_to_first_revenue"
    UPFRONT_CAPITAL = "upfront_capital"
    AUTOMATION = "automation"
    SMALL_TEAM_FEASIBILITY = "small_team_feasibility"
    ACQUISITION_DIFFICULTY = "acquisition_difficulty"
    COMPETITIVE_INTENSITY = "competitive_intensity"
    DEFENSIBILITY = "defensibility"
    WHY_NOW = "why_now"
    MARKET_ATTRACTIVENESS = "market_attractiveness"
    CUSTOMER_PAIN = "customer_pain"
    WILLINGNESS_TO_PAY = "willingness_to_pay"
    SCALABILITY = "scalability"
    EVIDENCE_QUALITY = "evidence_quality"
    ASSUMPTION_RISK = "assumption_risk"


class Verdict(StrEnum):
    PURSUE = "pursue"
    PROMISING_BUT_UNPROVEN = "promising_but_unproven"
    PARK = "park"
    KILL = "kill"


class Override(StrEnum):
    """Non-compensatory overrides; any of them forces a Kill (specs.md §17.4)."""

    CAPITAL_GATE = "capital_gate"
    TIME_TO_REVENUE_GATE = "time_to_revenue_gate"
    SPECIFICITY_GATE = "specificity_gate"
    FATAL_FLAW = "fatal_flaw"


class RunMode(StrEnum):
    OPEN_ENDED = "open_ended"
    CONSTRAINED = "constrained"


class PreferenceOrigin(StrEnum):
    """What operator action produced a learned preference signal (intent.md §19.2)."""

    CHECKPOINT_VETO = "checkpoint_veto"
    CHECKPOINT_PICK = "checkpoint_pick"
    DISCARD = "discard"
    PARK = "park"


class PreferenceSentiment(StrEnum):
    LIKE = "like"
    DISLIKE = "dislike"
    NEUTRAL = "neutral"


class CheckpointMode(StrEnum):
    MANDATORY = "mandatory"
    AUTONOMOUS = "autonomous"


class CheckpointAction(StrEnum):
    """What the operator decided at the checkpoint (specs.md §2.7)."""

    APPROVE = "approve"  # deep-research the top-ranked candidate
    PICK = "pick"  # re-pick a runner-up instead (needs a one-line reason)
    VETO = "veto"  # reject the whole shortlist (needs a one-line reason)
    AUTO = "auto"  # autonomous mode selected the top-ranked candidate
    PROMOTE = "promote"  # a later run promoted a shortlisted runner-up to deep research


class RunStatus(StrEnum):
    """Run state machine (specs.md §19.1)."""

    CREATED = "created"
    DISCOVERING = "discovering"
    SCREENING = "screening"
    SHALLOW_VALIDATION = "shallow_validation"
    AWAITING_CHECKPOINT = "awaiting_checkpoint"
    DEEP_RESEARCH = "deep_research"
    SYNTHESIZING = "synthesizing"
    BUILDING_PLAN = "building_plan"
    RED_TEAM = "red_team"
    EVALUATING = "evaluating"
    # A standalone run that re-verifies an existing opportunity's stale claims (specs.md
    # §2.11); it lives outside the funnel, so it is not part of the funnel state machine.
    REFRESHING = "refreshing"
    COMPLETED = "completed"
    COMPLETED_PARTIAL = "completed_partial"
    CANCELLED = "cancelled"
    HALTED_BUDGET = "halted_budget"
    HALTED_ERROR = "halted_error"


TERMINAL_RUN_STATUSES = frozenset(
    {RunStatus.COMPLETED, RunStatus.COMPLETED_PARTIAL, RunStatus.CANCELLED}
)
"""Halted runs are not terminal: they can be resumed (specs.md §19.4)."""


class StageName(StrEnum):
    DISCOVERY = "discovery"
    SCREENING = "screening"
    SHALLOW_VALIDATION = "shallow_validation"
    CHECKPOINT = "checkpoint"
    DEEP_RESEARCH = "deep_research"
    SYNTHESIS = "synthesis"
    STRATEGY = "strategy"
    RED_TEAM = "red_team"
    EVALUATION = "evaluation"
    # Re-verify an existing opportunity's stale claims (specs.md §2.11); not a funnel stage.
    REFRESH = "refresh"


class StageStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class AgentRunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    DEGRADED = "degraded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    # Stopped because the agent's or the run's budget can't afford the next call; the
    # pending sub-tasks stay in the ledger so a top-up can resume it (intent.md §21.3).
    HALTED_BUDGET = "halted_budget"


class ModelTier(StrEnum):
    CHEAP = "cheap"
    MEDIUM = "medium"
    PREMIUM = "premium"


class TraceKind(StrEnum):
    LLM = "llm"
    SEARCH = "search"
    FETCH = "fetch"
