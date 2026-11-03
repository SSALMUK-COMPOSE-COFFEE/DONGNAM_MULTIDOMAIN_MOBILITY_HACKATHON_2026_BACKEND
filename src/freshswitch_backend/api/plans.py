from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field
from sqlalchemy import select

from freshswitch_backend.api.auth import OptionalPrincipal, app_settings, current_org_id
from freshswitch_backend.api.computation import (
    compute_replan,
    explanation_budget,
    request_deadline,
)
from freshswitch_backend.api.vessels import (
    VesselClientFactory,
    fetch_busan_movements,
    vessel_client_factory,
)
from freshswitch_backend.data.loader import get_scenario
from freshswitch_backend.domain.models import Lot, Scenario
from freshswitch_backend.execution.cancellation import annotate_cancellations
from freshswitch_backend.execution.models import (
    ConfirmRequest,
    ExecutionOrder,
    RequestResultBody,
)
from freshswitch_backend.intake.tenants import load_catalog, tenant_scenario
from freshswitch_backend.integrations import (
    MAX_GAP_HOURS,
    PlannedVoyage,
    ScheduleChange,
    VesselMovement,
    detect_schedule_changes,
)
from freshswitch_backend.lots import ANY_ORG, LotFilter, RegisteredLot, lot_store
from freshswitch_backend.persistence import database
from freshswitch_backend.persistence.tables import DEMO_ORG, LotRow, OrganizationRow
from freshswitch_backend.planning.store import (
    HistoryEntry,
    IdempotencyMismatch,
    OrderInputError,
    PlanSession,
    PlanSummary,
    SessionConflict,
    store,
)
from freshswitch_backend.quality import with_learned_loss
from freshswitch_backend.replan import DelayEvent, Event, ReplanResult, replan
from freshswitch_backend.replan.explanation import explain_replan, llm_enabled
from freshswitch_backend.replan.models import AlternativeId

router = APIRouter(prefix="/plans", tags=["plans"])

OrgId = Annotated[str, Depends(current_org_id)]
INLINE_LOTS_FORBIDDEN = "SaaS 계획은 등록 출하(lot_ids)로만 만들 수 있습니다"


class CreatePlanRequest(BaseModel):
    scenario_id: str = "default"
    use_learned_loss: bool = False
    lots: list[Lot] = Field(default_factory=list)
    lot_ids: list[str] = Field(default_factory=list)
    include_registered: bool = False


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
    max_gap_hours: float = Field(default=MAX_GAP_HOURS, gt=0)


class ScheduleCheckResponse(BaseModel):
    plan_id: str
    source: str
    checked_voyage_ids: list[str]
    movement_count: int
    changes: list[ScheduleChange]
    events: list[DelayEvent]


def _session(plan_id: str, org_id: str = DEMO_ORG) -> PlanSession:
    session = store.get(plan_id, org_id=org_id)
    if session is None:
        raise HTTPException(status_code=404, detail="계획을 찾을 수 없습니다")
    return session


def _fingerprint(body: BaseModel) -> str:
    return body.model_dump_json(exclude={"idempotency_key"})


def _replayed(
    plan_id: str,
    key: str | None,
    kind: str,
    body: BaseModel,
    model=None,
    org_id: str = DEMO_ORG,
):
    if key is None:
        return None
    try:
        return store.idempotent(plan_id, key, kind, _fingerprint(body), model, org_id=org_id)
    except IdempotencyMismatch as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _remember(
    plan_id: str, key: str | None, kind: str, body: BaseModel, value, org_id: str = DEMO_ORG
):
    if key is None:
        return value
    try:
        return store.remember(plan_id, key, kind, _fingerprint(body), value, org_id=org_id)
    except IdempotencyMismatch as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _lot_errors(scenario: Scenario, lots: list[Lot]) -> list[str]:
    products = {p.id for p in scenario.products}
    shippers = {s.id for s in scenario.shippers}
    ports = {p.id for p in scenario.ports}
    capacity = max((c.capacity_pallets for c in scenario.containers), default=20)
    errors: list[str] = []
    for lot in lots:
        if lot.product_id not in products:
            errors.append(f"{lot.id}: 시나리오에 없는 품목입니다: {lot.product_id}")
        if lot.shipper_id not in shippers:
            errors.append(f"{lot.id}: 시나리오에 없는 화주입니다: {lot.shipper_id}")
        if lot.destination not in ports:
            errors.append(f"{lot.id}: 시나리오에 없는 항구입니다: {lot.destination}")
        if lot.pallets > capacity:
            errors.append(f"{lot.id}: 컨테이너 1대 용량({capacity}파렛트)을 넘습니다")
        if lot.sell_by_date < lot.ready_date:
            errors.append(f"{lot.id}: 판매기한이 출하 가능일보다 빠릅니다")
    return errors


def _shipper_org_lots(lots: list[RegisteredLot]) -> list[RegisteredLot]:
    if not lots:
        return []
    with database.begin() as db:
        owned = set(
            db.scalars(
                select(LotRow.id)
                .join(OrganizationRow, OrganizationRow.id == LotRow.shipper_org_id)
                .where(LotRow.id.in_([lot.id for lot in lots]), OrganizationRow.type == "shipper")
            )
        )
    return [lot for lot in lots if lot.id in owned]


def _with_lots(
    scenario: Scenario,
    body: CreatePlanRequest,
    lot_org: str = DEMO_ORG,
    registered_only: bool = False,
    shipper_orgs_only: bool = False,
) -> tuple[Scenario, list[str]]:
    registered = lot_store.get_many(list(dict.fromkeys(body.lot_ids)), org_id=lot_org)
    if shipper_orgs_only:
        registered = _shipper_org_lots(registered)
    missing = sorted(set(body.lot_ids) - {lot.id for lot in registered})
    if missing:
        raise HTTPException(
            status_code=422, detail=f"등록되지 않은 로트입니다: {', '.join(missing)}"
        )
    foreign = [lot.id for lot in registered if lot.scenario_id != scenario.id]
    if foreign:
        raise HTTPException(
            status_code=422,
            detail=f"다른 시나리오에 등록된 로트입니다: {', '.join(foreign)}",
        )
    taken = [lot.id for lot in registered if lot.status != "registered"]
    if registered_only and taken:
        raise HTTPException(
            status_code=409,
            detail=f"이미 계획에 들어갔거나 취소된 출하입니다: {', '.join(taken)}",
        )
    if body.include_registered:
        chosen = {lot.id for lot in registered}
        extra = [
            lot
            for lot in lot_store.search(
                LotFilter(scenario_id=scenario.id, limit=10_000), org_id=lot_org
            )
            if lot.status == "registered" and lot.id not in chosen
        ]
        registered += _shipper_org_lots(extra) if shipper_orgs_only else extra
    added = [*body.lots, *(lot.to_lot() for lot in registered)]
    if not added:
        return scenario, []
    seen = {lot.id for lot in scenario.lots}
    clashes: list[str] = []
    for lot in added:
        if lot.id in seen:
            clashes.append(lot.id)
        seen.add(lot.id)
    if clashes:
        raise HTTPException(
            status_code=422,
            detail=f"로트 ID가 겹칩니다(시나리오·등록·직접 입력 로트 중): {', '.join(clashes)}",
        )
    errors = _lot_errors(scenario, added)
    if errors:
        raise HTTPException(status_code=422, detail=errors)
    merged = scenario.model_copy(update={"lots": [*scenario.lots, *added]})
    return merged, [lot.id for lot in registered]


def _base_scenario(scenario_id: str) -> Scenario:
    scenario = get_scenario(scenario_id)
    if scenario is None:
        raise HTTPException(status_code=404, detail="시나리오를 찾을 수 없습니다")
    return scenario


def create_tenant_plan(body: CreatePlanRequest, org_id: str) -> PlanSession:
    if body.lots:
        raise HTTPException(status_code=422, detail=INLINE_LOTS_FORBIDDEN)
    base = _base_scenario(body.scenario_id)
    scenario = tenant_scenario(base, load_catalog(ANY_ORG, base))
    scenario, registered_ids = _with_lots(
        scenario, body, lot_org=ANY_ORG, registered_only=True, shipper_orgs_only=True
    )
    if not registered_ids:
        raise HTTPException(status_code=422, detail="계획에 넣을 등록 출하가 없습니다")
    if body.use_learned_loss:
        scenario = with_learned_loss(scenario)
    return store.create(scenario, registered_ids, org_id=org_id, lot_org_id=ANY_ORG)


@router.post("", status_code=201)
def create_plan(
    body: CreatePlanRequest, request: Request, principal: OptionalPrincipal
) -> PlanSession:
    scenario = _base_scenario(body.scenario_id)
    if body.lots and app_settings(request).saas_mode:
        raise HTTPException(status_code=422, detail=INLINE_LOTS_FORBIDDEN)
    if principal is not None:
        if principal.org_type != "platform":
            raise HTTPException(status_code=403, detail="계획은 플랫폼 조직만 만들 수 있습니다")
        return create_tenant_plan(body, principal.org_id)
    scenario, registered_ids = _with_lots(scenario, body)
    if body.use_learned_loss:
        scenario = with_learned_loss(scenario)
    return store.create(scenario, registered_ids)


@router.get("")
def list_plans(
    org_id: OrgId,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[PlanSummary]:
    return store.summaries(limit, offset, org_id=org_id)


@router.get("/{plan_id}")
def read_plan(plan_id: str, org_id: OrgId) -> PlanSession:
    return _session(plan_id, org_id)


@router.delete("/{plan_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_plan(plan_id: str, org_id: OrgId) -> None:
    if not store.delete(plan_id, org_id=org_id):
        raise HTTPException(status_code=404, detail="계획을 찾을 수 없습니다")


@router.post("/{plan_id}/events")
async def post_event(
    plan_id: str,
    body: EventRequest,
    org_id: OrgId,
    llm: Annotated[bool | None, Query()] = None,
) -> ReplanResponse:
    deadline = request_deadline()
    session = await run_in_threadpool(_session, plan_id, org_id)
    replayed = await run_in_threadpool(
        _replayed, plan_id, body.idempotency_key, "events", body, ReplanResponse, org_id
    )
    if replayed is not None:
        return replayed
    result = await compute_replan(
        replan,
        session.scenario,
        session.plan,
        body.event,
        air_locked=session.air_locked_lot_ids,
        deadline=deadline,
    )
    annotate_cancellations(result, session.scenario, session.plan)
    result.explanation = await explain_replan(
        session.scenario,
        session.plan,
        result,
        use_llm=llm_enabled(llm),
        timeout_s=explanation_budget(deadline),
    )
    pending = await run_in_threadpool(store.add_pending, session, result, org_id)
    response = ReplanResponse(
        **{name: getattr(result, name) for name in ReplanResult.model_fields},
        plan_id=plan_id,
        replan_id=pending.replan_id,
        base_version=pending.base_version,
    )
    return await run_in_threadpool(
        _remember, plan_id, body.idempotency_key, "events", body, response, org_id
    )


@router.post("/{plan_id}/apply")
def apply_alternative(plan_id: str, body: ApplyRequest, org_id: OrgId) -> PlanSession:
    _session(plan_id, org_id)
    key = body.idempotency_key
    try:
        return store.apply(
            plan_id,
            body.alternative_id,
            body.replan_id,
            idempotency=(key, _fingerprint(body)) if key is not None else None,
            org_id=org_id,
        )
    except IdempotencyMismatch as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except SessionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(
            status_code=404, detail="재최적화 결과 또는 대안을 찾을 수 없습니다"
        ) from exc


@router.get("/{plan_id}/orders")
def list_orders(plan_id: str, org_id: OrgId) -> list[ExecutionOrder]:
    _session(plan_id, org_id)
    return store.orders(plan_id, org_id=org_id)


@router.get("/{plan_id}/orders/{order_id}")
def read_order(plan_id: str, order_id: str, org_id: OrgId) -> ExecutionOrder:
    _session(plan_id, org_id)
    try:
        return store.order(plan_id, order_id, org_id=org_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="실행 지시를 찾을 수 없습니다") from exc


@router.post("/{plan_id}/orders/{order_id}/confirm")
def confirm_order(
    plan_id: str, order_id: str, body: ConfirmRequest, org_id: OrgId
) -> ExecutionOrder:
    _session(plan_id, org_id)
    try:
        return store.confirm(plan_id, order_id, body, org_id=org_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="실행 지시를 찾을 수 없습니다") from exc
    except OrderInputError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except SessionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/{plan_id}/orders/{order_id}/requests/{request_id}")
def answer_request(
    plan_id: str, order_id: str, request_id: str, body: RequestResultBody, org_id: OrgId
) -> ExecutionOrder:
    _session(plan_id, org_id)
    try:
        return store.answer_request(plan_id, order_id, request_id, body, org_id=org_id)
    except KeyError as exc:
        raise HTTPException(
            status_code=404, detail="실행 지시 또는 요청을 찾을 수 없습니다"
        ) from exc
    except SessionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/{plan_id}/history")
def read_history(plan_id: str, org_id: OrgId) -> list[HistoryEntry]:
    return _session(plan_id, org_id).history


@router.post("/{plan_id}/schedule-check")
async def schedule_check(
    plan_id: str,
    factory: Annotated[VesselClientFactory, Depends(vessel_client_factory)],
    org_id: OrgId,
    body: ScheduleCheckRequest | None = None,
    demo: Annotated[bool, Query()] = False,
) -> ScheduleCheckResponse:
    session = await run_in_threadpool(_session, plan_id, org_id)
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
        max_gap_hours=body.max_gap_hours,
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
