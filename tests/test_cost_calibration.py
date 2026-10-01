"""Cost calibration (plan.md §24 Phase 8 accept: "cost calibrated to ≤ $20").

The worst case a run can spend is the sum of the per-agent budget dollar caps across every
agent that runs. These tests project that from the *default* configuration — offline, so
the worst case is a proven property, not a hope. Real spend is far lower (the caps are
ceilings with headroom for re-asks, not expected values); the runtime's budget guard also
halts before the ceiling either way.

Discovery grew from 5 to 10 signal sources (5 B2B/SMB + 5 consumer-app, added side by side
rather than swapped in). That alone pushes the *default* worst case over $20 — the ceiling
is no longer guaranteed to hold out of the box; an operator who wants the old guarantee
must narrow ``discovery_scope.signal_sources`` or raise the run's own budget ceiling. The
first test below documents that deliberate tradeoff rather than asserting it away.
"""

from collections.abc import Iterator

import pytest
from sqlalchemy.orm import Session, sessionmaker

from opp.config.settings import Settings
from opp.db.session import create_db_engine, create_session_factory
from opp.domain.enums import StageName
from opp.domain.report import Trace
from opp.llm.factory import build_gateway
from opp.observability.calibration import project_cost, render_projection
from opp.orchestration.stages import StageServices
from opp.orchestration.wiring import build_stage_services
from opp.research.fake import FakeSearchProvider


@pytest.fixture
def sessions() -> Iterator[sessionmaker[Session]]:
    engine = create_db_engine("sqlite://")  # in-memory; the projection touches no tables
    yield create_session_factory(engine)
    engine.dispose()


def _discard_trace(_trace: Trace) -> None:
    """A no-op sink: the projection makes no calls."""


def _services(sessions: sessionmaker[Session]) -> StageServices:
    """The funnel services the CLI/API build from default settings. Providers are unused by
    the projection, so a no-op sink and a fake search stand in (as ``opp calibrate`` does)."""
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    return build_stage_services(
        settings,
        sessions=sessions,
        gateway=build_gateway(settings, _discard_trace),
        search=FakeSearchProvider(),
        sink=_discard_trace,
    )


def test_default_config_worst_case_now_exceeds_the_20_ceiling(
    sessions: sessionmaker[Session],
) -> None:
    """Ten signal sources (plan.md §24 was calibrated for five) push the default worst case
    to $22.75 — over the original $20 ceiling. This is the known, accepted tradeoff of
    running the B2B and consumer-app recipes side by side (see module docstring); the
    runtime budget guard still halts before any real run actually overspends."""
    projection = project_cost(_services(sessions))

    assert not projection.within_ceiling
    assert projection.max_usd == pytest.approx(22.75)
    assert projection.reserve_usd == pytest.approx(-2.75)
    # The projected floor equals the pre-flight minimum (sum of the stage minimums) — the
    # per-stage minimums are flat, so they're unaffected by the signal-source count.
    assert projection.minimum_usd == pytest.approx(15.5)


def test_per_stage_projection_fans_out_and_fits_the_specs_bands(
    sessions: sessionmaker[Session],
) -> None:
    by_stage = {s.stage: s for s in project_cost(_services(sessions)).stages}

    # Discovery fans out to one agent per signal source (10, now that the consumer-app
    # sources run alongside the original 5); deep research to one per axis.
    assert by_stage[StageName.DISCOVERY].instances == 10
    assert by_stage[StageName.DISCOVERY].max_usd == pytest.approx(7.5)  # over specs.md's $3-4
    assert by_stage[StageName.DEEP_RESEARCH].instances == 4
    assert by_stage[StageName.DEEP_RESEARCH].max_usd == pytest.approx(6.0)  # $5-7
    # Red-team + judge share the specs $2-3 band.
    shared = by_stage[StageName.RED_TEAM].max_usd + by_stage[StageName.EVALUATION].max_usd
    assert shared == pytest.approx(3.0)
    # The checkpoint has no agent and so no projected cost.
    assert StageName.CHECKPOINT not in by_stage


def test_a_deep_research_kickback_is_reported_and_may_exceed_the_ceiling(
    sessions: sessionmaker[Session],
) -> None:
    projection = project_cost(_services(sessions))
    # One kickback re-runs the four specialists and synthesis: +$6 +$1 (unaffected by the
    # discovery-stage signal-source count).
    assert projection.kickback_extra_usd == pytest.approx(7.0)
    assert projection.max_with_kickback_usd == pytest.approx(29.75)  # guarded by the run budget


def test_the_gate_is_meaningful_it_flips_when_the_worst_case_would_overrun(
    sessions: sessionmaker[Session],
) -> None:
    services = _services(sessions)
    # A ceiling below the worst case is flagged.
    assert not project_cost(services, ceiling_usd=10.0).within_ceiling
    # The full 10-source default is over $20 (see test above); narrowing back to the
    # original 5 B2B sources brings the worst case within it again.
    assert project_cost(services, signal_sources=5).within_ceiling
    # And 8 (all 5 B2B plus 3 consumer-app) is still over.
    assert not project_cost(services, signal_sources=8).within_ceiling


def test_render_projection_shows_the_verdict_and_totals(
    sessions: sessionmaker[Session],
) -> None:
    text = render_projection(project_cost(_services(sessions)))
    assert "discovery" in text
    assert "$22.75 of the $20.00 ceiling (OVER)" in text
    assert "kickback" in text

    within = render_projection(project_cost(_services(sessions), ceiling_usd=25.0))
    assert "(within)" in within
