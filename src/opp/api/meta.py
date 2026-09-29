"""Profile and configuration endpoints (plan.md §11): what the New Run form offers."""

from fastapi import APIRouter

from opp.api.dtos import ConfigResponse, ProfileDTO
from opp.api.engine import EngineDep
from opp.config.profiles import PROFILES
from opp.domain.enums import BusinessShape, CheckpointMode, Dimension, RunMode, SignalSource
from opp.domain.run import DEFAULT_BUDGET_USD, DEFAULT_PROFILE

router = APIRouter(tags=["config"])


@router.get("/profiles")
def list_profiles() -> list[ProfileDTO]:
    return [ProfileDTO.of(profile) for profile in PROFILES.values()]


@router.get("/config")
def get_config(engine: EngineDep) -> ConfigResponse:
    return ConfigResponse(
        profiles=[ProfileDTO.of(profile) for profile in PROFILES.values()],
        default_profile=DEFAULT_PROFILE,
        default_budget_usd=DEFAULT_BUDGET_USD,
        stage_minimums_usd={
            stage.value: amount for stage, amount in engine.stage_minimums_usd.items()
        },
        signal_sources=[s.value for s in SignalSource],
        business_shapes=[s.value for s in BusinessShape],
        checkpoint_modes=[m.value for m in CheckpointMode],
        run_modes=[m.value for m in RunMode],
        dimensions=[d.value for d in Dimension],
    )
