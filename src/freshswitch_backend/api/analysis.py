from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from freshswitch_backend.analysis import (
    DEFAULT_FORWARDER_SHARE,
    DEFAULT_OPS_COST_PER_VOYAGE,
    BenchmarkResult,
    BusinessResult,
    SensitivityResult,
    benchmark,
    business,
    sensitivity,
)
from freshswitch_backend.data.loader import get_scenario
from freshswitch_backend.domain.models import Scenario
from freshswitch_backend.planning.store import store

router = APIRouter(prefix="/analysis", tags=["analysis"])


class SensitivityRequest(BaseModel):
    scenario_id: str = "default"
    plan_id: str | None = None


class BenchmarkRequest(BaseModel):
    scenario_id: str = "default"


def _scenario(scenario_id: str) -> Scenario:
    scenario = get_scenario(scenario_id)
    if scenario is None:
        raise HTTPException(status_code=404, detail="시나리오를 찾을 수 없습니다")
    return scenario


@router.post("/sensitivity")
def run_sensitivity(body: SensitivityRequest) -> SensitivityResult:
    if body.plan_id is not None:
        session = store.get(body.plan_id)
        if session is None:
            raise HTTPException(status_code=404, detail="계획을 찾을 수 없습니다")
        return sensitivity(session.scenario, plan_id=body.plan_id)
    return sensitivity(_scenario(body.scenario_id))


@router.post("/benchmark")
def run_benchmark(body: BenchmarkRequest) -> BenchmarkResult:
    return benchmark(_scenario(body.scenario_id))


@router.get("/business")
def run_business(
    scenario_id: str = "default",
    forwarder_share: float = Query(DEFAULT_FORWARDER_SHARE, ge=0, le=1),
    ops_cost_per_voyage: int = Query(DEFAULT_OPS_COST_PER_VOYAGE, ge=0),
) -> BusinessResult:
    return business(_scenario(scenario_id), forwarder_share, ops_cost_per_voyage)
