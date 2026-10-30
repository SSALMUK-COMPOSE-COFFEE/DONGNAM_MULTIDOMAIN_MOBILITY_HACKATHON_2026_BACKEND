from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from freshswitch_backend.api.vessels import (
    VesselClientFactory,
    fetch_busan_movements,
    vessel_client_factory,
)
from freshswitch_backend.data.loader import get_scenario
from freshswitch_backend.execution.cancellation import annotate_cancellations
from freshswitch_backend.execution.models import (
    ConfirmRequest,
    ExecutionOrder,
    RequestResultBody,
)
from freshswitch_backend.integrations import (
    PlannedVoyage,
    ScheduleChange,
    VesselMovement,
    detect_schedule_changes,
)
from freshswitch_backend.planning.store import (
    HistoryEntry,
    IdempotencyMismatch,
    OrderInputError,
    PlanSession,
    SessionConflict,
    store,
)
from freshswitch_backend.replan import DelayEvent, Event, ReplanResult, replan
from freshswitch_backend.replan.explanation import explain_replan, llm_enabled
from freshswitch_backend.replan.models import AlternativeId

router = APIRouter(prefix="/plans", tags=["plans"])


class CreatePlanRequest(BaseModel):
    scenario_id: str = "default"


class EventRequest(BaseModel):
    event: Event
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=200)


class ReplanResponse(ReplanResult):
    plan_id: str
    replan_id: str
    base_version: int


class ApplyRequest(BaseModel):
    alternative_id: AlternativeId
    replan_id: str | None = None
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=200)


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


def _fingerprint(body: BaseModel) -> str:
    return body.model_dump_json(exclude={"idempotency_key"})


def _replayed(plan_id: str, key: str | None, kind: str, body: BaseModel):
    if key is None:
        return None
    try:
        return store.idempotent(plan_id, key, kind, _fingerprint(body))
    except IdempotencyMismatch as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _remember(plan_id: str, key: str | None, kind: str, body: BaseModel, value):
    if key is None:
        return value
    try:
        return store.remember(plan_id, key, kind, _fingerprint(body), value)
    except IdempotencyMismatch as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


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
async def post_event(
    plan_id: str,
    body: EventRequest,
    llm: Annotated[bool | None, Query()] = None,
) -> ReplanResponse:
    session = _session(plan_id)
    replayed = _replayed(plan_id, body.idempotency_key, "events", body)
    if replayed is not None:
        return replayed
    result = await run_in_threadpool(
        replan,
        session.scenario,
        session.plan,
        body.event,
        air_locked=session.air_locked_lot_ids,
    )
    annotate_cancellations(result, session.scenario, session.plan)
    result.explanation = await explain_replan(
        session.scenario, session.plan, result, use_llm=llm_enabled(llm)
    )
    pending = store.add_pending(session, result)
    response = ReplanResponse(
        **{name: getattr(result, name) for name in ReplanResult.model_fields},
        plan_id=plan_id,
        replan_id=pending.replan_id,
        base_version=pending.base_version,
    )
    return _remember(plan_id, body.idempotency_key, "events", body, response)


@router.post("/{plan_id}/apply")
def apply_alternative(plan_id: str, body: ApplyRequest) -> PlanSession:
    _session(plan_id)
    replayed = _replayed(plan_id, body.idempotency_key, "apply", body)
    if replayed is not None:
        return replayed
    try:
        session = store.apply(plan_id, body.alternative_id, body.replan_id)
    except SessionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(
            status_code=404, detail="재최적화 결과 또는 대안을 찾을 수 없습니다"
        ) from exc
    return _remember(plan_id, body.idempotency_key, "apply", body, session)


@router.get("/{plan_id}/orders")
def list_orders(plan_id: str) -> list[ExecutionOrder]:
    _session(plan_id)
    return store.orders(plan_id)


@router.get("/{plan_id}/orders/{order_id}")
def read_order(plan_id: str, order_id: str) -> ExecutionOrder:
    _session(plan_id)
    try:
        return store.order(plan_id, order_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="실행 지시를 찾을 수 없습니다") from exc


@router.post("/{plan_id}/orders/{order_id}/confirm")
def confirm_order(plan_id: str, order_id: str, body: ConfirmRequest) -> ExecutionOrder:
    _session(plan_id)
    try:
        return store.confirm(plan_id, order_id, body)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="실행 지시를 찾을 수 없습니다") from exc
    except OrderInputError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except SessionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/{plan_id}/orders/{order_id}/requests/{request_id}")
def answer_request(
    plan_id: str, order_id: str, request_id: str, body: RequestResultBody
) -> ExecutionOrder:
    _session(plan_id)
    try:
        return store.answer_request(plan_id, order_id, request_id, body)
    except KeyError as exc:
        raise HTTPException(
            status_code=404, detail="실행 지시 또는 요청을 찾을 수 없습니다"
        ) from exc
    except SessionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/{plan_id}/history")
def read_history(plan_id: str) -> list[HistoryEntry]:
    return _session(plan_id).history


@router.post("/{plan_id}/schedule-check")
async def schedule_check(
    plan_id: str,
    factory: Annotated[VesselClientFactory, Depends(vessel_client_factory)],
    body: ScheduleCheckRequest | None = None,
    demo: Annotated[bool, Query()] = False,
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
        movements = await fetch_busan_movements(factory, start, end, "O", demo=demo)
        source = "해수부"
    changes = detect_schedule_changes(
        [PlannedVoyage(voyage_id=v.id, vessel_name=v.vessel_name, etd=v.etd) for v in voyages],
        movements,
        threshold_hours=body.threshold_hours,
    )
    events = [
        DelayEvent(
            voyage_id=change.voyage_id,
            new_etd=change.observed_etd,
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
