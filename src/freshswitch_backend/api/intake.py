from fastapi import APIRouter, HTTPException

from freshswitch_backend.data.loader import get_scenario
from freshswitch_backend.intake.models import IntakePreview, IntakeRequest
from freshswitch_backend.intake.service import IntakeError, preview_lot
from freshswitch_backend.planning.store import store

router = APIRouter(prefix="/intake", tags=["intake"])


@router.post("/lots")
def intake_lot(body: IntakeRequest) -> IntakePreview:
    plan = None
    if body.plan_id is not None:
        session = store.get(body.plan_id)
        if session is None:
            raise HTTPException(status_code=404, detail="계획을 찾을 수 없습니다")
        scenario, plan = session.scenario, session.plan
    else:
        scenario = get_scenario(body.scenario_id)
        if scenario is None:
            raise HTTPException(status_code=404, detail="시나리오를 찾을 수 없습니다")
    try:
        return preview_lot(scenario, body, plan)
    except IntakeError as exc:
        raise HTTPException(status_code=422, detail=[e.model_dump() for e in exc.errors]) from exc
