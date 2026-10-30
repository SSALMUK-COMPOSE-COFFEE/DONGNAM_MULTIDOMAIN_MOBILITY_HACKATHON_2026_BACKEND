from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from freshswitch_backend.api.vessels import (
    VesselClientFactory,
    fetch_busan_movements,
    vessel_client_factory,
)
from freshswitch_backend.data.loader import get_scenario
from freshswitch_backend.integrations import (
    PlannedVoyage,
    ScheduleChange,
    VesselMovement,
    detect_schedule_changes,
)
from freshswitch_backend.planning.store import (
    HistoryEntry,
    PlanSession,
    SessionConflict,
    store,
)
from freshswitch_backend.replan import DelayEvent, Event, ReplanResult, replan
from freshswitch_backend.replan.models import AlternativeId

router = APIRouter(prefix="/plans", tags=["plans"])


class CreatePlanRequest(BaseModel):
    scenario_id: str = "default"


class EventRequest(BaseModel):
    event: Event


class ReplanResponse(ReplanResult):
    plan_id: str
    replan_id: str
    base_version: int


class ApplyRequest(BaseModel):
    alternative_id: AlternativeId
    replan_id: str | None = None


class ScheduleCheckRequest(BaseModel):
    movements: list[VesselMovement] | None = None
    threshold_hours: float = 3.0


class ScheduleCheckResponse(BaseModel):
    plan_id: str
    source: str
    checked_voyage_ids: list[str]
    movement_count: int
    changes: list[ScheduleChange]
    events: list[DelayEvent]


def _session(plan_id: str) -> PlanSession:
    session = store.get(plan_id)
    if session is None:
        raise HTTPException(status_code=404, detail="계획을 찾을 수 없습니다")
    return session


@router.post("", status_code=201)
def create_plan(body: CreatePlanRequest) -> PlanSession:
    scenario = get_scenario(body.scenario_id)
    if scenario is None:
        raise HTTPException(status_code=404, detail="시나리오를 찾을 수 없습니다")
    return store.create(scenario)


@router.get("/{plan_id}")
def read_plan(plan_id: str) -> PlanSession:
    return _session(plan_id)


@router.post("/{plan_id}/events")
def post_event(plan_id: str, body: EventRequest) -> ReplanResponse:
    session = _session(plan_id)
    result = replan(
        session.scenario,
        session.plan,
        body.event,
        air_locked=session.air_locked_lot_ids,
    )
    pending = store.add_pending(session, result)
    return ReplanResponse(
        **{name: getattr(result, name) for name in ReplanResult.model_fields},
        plan_id=plan_id,
        replan_id=pending.replan_id,
        base_version=pending.base_version,
    )


@router.post("/{plan_id}/apply")
def apply_alternative(plan_id: str, body: ApplyRequest) -> PlanSession:
    _session(plan_id)
    try:
        return store.apply(plan_id, body.alternative_id, body.replan_id)
    except SessionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(
            status_code=404, detail="재최적화 결과 또는 대안을 찾을 수 없습니다"
        ) from exc


@router.get("/{plan_id}/history")
def read_history(plan_id: str) -> list[HistoryEntry]:
    return _session(plan_id).history


@router.post("/{plan_id}/schedule-check")
async def schedule_check(
    plan_id: str,
    factory: Annotated[VesselClientFactory, Depends(vessel_client_factory)],
    body: ScheduleCheckRequest | None = None,
) -> ScheduleCheckResponse:
    session = _session(plan_id)
    body = body or ScheduleCheckRequest()
    used = {c.voyage_id for c in session.plan.containers if c.used}
    voyages = [v for v in session.scenario.voyages if v.id in used] or session.scenario.voyages
    if body.movements is not None:
        movements, source = body.movements, "provided"
    else:
        start = min(v.etd for v in voyages).date() - timedelta(days=1)
        end = max(v.etd for v in voyages).date() + timedelta(days=3)
        movements = await fetch_busan_movements(factory, start, end, "O")
        source = "해수부"
    changes = detect_schedule_changes(
        [PlannedVoyage(voyage_id=v.id, vessel_name=v.vessel_name, etd=v.etd) for v in voyages],
        movements,
        threshold_hours=body.threshold_hours,
    )
    by_id = {v.id: v for v in voyages}
    events = [
        DelayEvent(
            voyage_id=change.voyage_id,
            new_etd=change.observed_etd,
            new_cy_cutoff=by_id[change.voyage_id].cy_cutoff
            + (change.observed_etd - change.planned_etd),
            source="해수부",
        )
        for change in changes
    ]
    return ScheduleCheckResponse(
        plan_id=plan_id,
        source=source,
        checked_voyage_ids=[v.id for v in voyages],
        movement_count=len(movements),
        changes=changes,
        events=events,
    )
