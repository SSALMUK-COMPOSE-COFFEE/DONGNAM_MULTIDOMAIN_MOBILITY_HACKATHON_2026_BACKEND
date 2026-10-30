from fastapi import APIRouter, HTTPException

from freshswitch_backend.data.loader import get_scenario, load_scenarios
from freshswitch_backend.domain.models import Scenario, ScenarioSummary

router = APIRouter(prefix="/scenarios", tags=["scenarios"])


@router.get("")
def list_scenarios() -> list[ScenarioSummary]:
    return [
        ScenarioSummary(id=s.id, name=s.name, description=s.description)
        for s in load_scenarios().values()
    ]


@router.get("/{scenario_id}")
def read_scenario(scenario_id: str) -> Scenario:
    scenario = get_scenario(scenario_id)
    if scenario is None:
        raise HTTPException(status_code=404, detail="scenario not found")
    return scenario
