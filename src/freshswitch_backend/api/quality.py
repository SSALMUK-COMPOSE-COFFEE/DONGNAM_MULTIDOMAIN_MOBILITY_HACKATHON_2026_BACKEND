from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.concurrency import run_in_threadpool
from fastapi.security import HTTPAuthorizationCredentials

from freshswitch_backend.api.access import bearer, require_demo_reset
from freshswitch_backend.api.auth import OptionalPrincipal, app_settings
from freshswitch_backend.data.loader import get_scenario
from freshswitch_backend.domain.models import Scenario
from freshswitch_backend.quality import (
    QualityAccessDenied,
    QualityInputError,
    learned_table,
    quality_history,
)
from freshswitch_backend.quality.impact import learning_impact
from freshswitch_backend.quality.models import (
    ImpactResult,
    LossTable,
    Observation,
    ObservationIn,
    ObservationList,
    ResetResult,
)

router = APIRouter(prefix="/quality", tags=["quality"])


def reset_org_type(
    request: Request,
    principal: OptionalPrincipal,
    credential: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> str | None:
    if principal is None and not app_settings(request).saas_mode:
        return None
    if credential is not None:
        require_demo_reset(credential)
        return None
    if principal is None:
        raise HTTPException(status_code=401, detail="로그인이 필요합니다")
    return principal.org_type


ResetOrgType = Annotated[str | None, Depends(reset_org_type)]


def _scenario(scenario_id: str) -> Scenario:
    scenario = get_scenario(scenario_id)
    if scenario is None:
        raise HTTPException(status_code=404, detail="시나리오를 찾을 수 없습니다")
    return scenario


@router.get("/loss-rates")
def loss_rates(scenario_id: Annotated[str, Query()] = "default") -> LossTable:
    return learned_table(_scenario(scenario_id))


@router.get("/observations")
def list_observations() -> ObservationList:
    observations = quality_history.observations()
    return ObservationList(
        seed_label=quality_history.seed_label,
        count=len(observations),
        observations=observations,
    )


@router.post("/observations", status_code=201)
def add_observation(body: ObservationIn) -> Observation:
    try:
        return quality_history.add(body)
    except QualityInputError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/observations/{obs_id}")
def read_observation(obs_id: str) -> Observation:
    observation = quality_history.get(obs_id)
    if observation is None:
        raise HTTPException(status_code=404, detail="관측을 찾을 수 없습니다")
    return observation


@router.delete("/observations/{obs_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_observation(obs_id: str, org_type: ResetOrgType) -> None:
    try:
        deleted = quality_history.delete(obs_id, org_type=org_type)
    except QualityAccessDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="관측을 찾을 수 없습니다")


@router.post("/reset")
def reset(org_type: ResetOrgType, seed: Annotated[bool, Query()] = True) -> ResetResult:
    try:
        cleared, seeded = quality_history.reset(seed=seed, org_type=org_type)
    except QualityAccessDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return ResetResult(cleared=cleared, seeded=seeded)


@router.get("/impact")
async def impact(scenario_id: Annotated[str, Query()] = "default") -> ImpactResult:
    scenario = _scenario(scenario_id)
    table = await run_in_threadpool(learned_table, scenario)
    cells = [c for c in table.cells if c.observations]
    return await run_in_threadpool(learning_impact, scenario, table.adjustments, cells)
