import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select, update

from freshswitch_backend.api.auth import Principal, require_role
from freshswitch_backend.api.computation import compute_replan, request_deadline
from freshswitch_backend.api.plans import (
    ApplyRequest,
    EventRequest,
    ReplanResponse,
    apply_alternative,
    post_event,
)
from freshswitch_backend.api.saas_access import PlanAccess, plan_for
from freshswitch_backend.domain.models import Lot
from freshswitch_backend.execution.cancellation import annotate_cancellations
from freshswitch_backend.notifications import (
    ChangeRequestNotice,
    Notification,
    list_for_org,
    notify_change_request_decision,
    notify_order_confirmation,
)
from freshswitch_backend.persistence import database
from freshswitch_backend.persistence.tables import ChangeRequestRow, LotRow, OrganizationRow
from freshswitch_backend.planning.store import PlanSession, store
from freshswitch_backend.replan import (
    AirSwitchEvent,
    CancelEvent,
    Event,
    ReduceEvent,
    ReplanResult,
    apply_event,
    replan,
)
from freshswitch_backend.replan.models import AlternativeId, CancellationCharge

router = APIRouter(prefix="/saas", tags=["saas-change-requests"])

ShipperPrincipal = Annotated[Principal, Depends(require_role("shipper"))]
PlatformPrincipal = Annotated[Principal, Depends(require_role("platform"))]

ChangeType = Literal["reduce", "cancel", "air_switch"]
ChangeStatus = Literal["open", "applied", "rejected"]

NOT_FOUND = "변경 요청을 찾을 수 없습니다"
LOT_NOT_IN_PLAN = "계획에 없는 출하입니다"
FOREIGN_LOT = "다른 화주의 출하는 변경을 요청할 수 없습니다"
NOT_PLANNED = "계획에 배정된(planned) 자기 출하만 변경을 요청할 수 있습니다"
ALREADY_OPEN = "이 출하에 처리 중인 변경 요청이 있습니다"
ALREADY_AIR = "이미 항공으로 배정된 출하입니다"
NOT_OPEN = "이미 처리된 변경 요청입니다"
NO_REPLAN = "먼저 변경 요청으로 3안을 계산하세요"


class ChangeRequestCreate(BaseModel):
    type: ChangeType
    lot_id: str = Field(min_length=1, max_length=120)
    new_pallets: int | None = Field(default=None, gt=0)
    reason: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def _pallets(self) -> "ChangeRequestCreate":
        if self.type == "reduce" and self.new_pallets is None:
            raise ValueError("감량 요청에는 new_pallets가 필요합니다")
        if self.type != "reduce" and self.new_pallets is not None:
            return self.model_copy(update={"new_pallets": None})
        return self


class CancellationEstimate(BaseModel):
    alternative_id: AlternativeId
    fee: int
    fee_rate: float
    cancelled_pallets: int
    refilled_pallets: int
    dead_pallets: int
    min_fee: int
    max_fee: int
    rule: str
    source: str


class ShipperChangeRequest(BaseModel):
    request_id: str
    plan_id: str
    lot_id: str
    shipper_id: str
    type: ChangeType
    new_pallets: int | None
    reason: str | None
    status: ChangeStatus
    cancellation_estimate: CancellationEstimate | None
    alternative_id: AlternativeId | None
    order_id: str | None
    decision_note: str | None
    created_at: datetime
    updated_at: datetime
    decided_at: datetime | None


class PlatformChangeRequest(ShipperChangeRequest):
    shipper_org_id: str
    shipper_org_name: str
    event: Event
    replan_id: str | None


class ChangeRequestReplan(BaseModel):
    change_request: PlatformChangeRequest
    replan: ReplanResponse


class ChangeRequestApply(BaseModel):
    alternative_id: AlternativeId
    note: str | None = Field(default=None, max_length=500)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=200)


class ChangeRequestReject(BaseModel):
    note: str | None = Field(default=None, max_length=500)


class ChangeRequestApplied(BaseModel):
    change_request: PlatformChangeRequest
    plan: PlanSession
    notifications: list[Notification]


def to_event(change_type: str, lot_id: str, new_pallets: int | None) -> Event:
    if change_type == "reduce":
        return ReduceEvent(lot_id=lot_id, new_pallets=new_pallets)
    if change_type == "cancel":
        return CancelEvent(lot_id=lot_id)
    return AirSwitchEvent(lot_id=lot_id)


def _estimate(result: ReplanResult) -> CancellationEstimate | None:
    charges: dict[str, CancellationCharge] = {
        a.id: a.diff.cancellation
        for a in result.alternatives
        if a.feasible and a.diff.cancellation is not None
    }
    if not charges:
        return None
    chosen_id = result.recommended_id if result.recommended_id in charges else next(iter(charges))
    chosen = charges[chosen_id]
    fees = [c.fee for c in charges.values()]
    return CancellationEstimate(
        alternative_id=chosen_id,
        fee=chosen.fee,
        fee_rate=chosen.fee_rate,
        cancelled_pallets=chosen.cancelled_pallets,
        refilled_pallets=chosen.refilled_pallets,
        dead_pallets=chosen.dead_pallets,
        min_fee=min(fees),
        max_fee=max(fees),
        rule=chosen.rule,
        source=chosen.source,
    )


def _shipper_view(row: ChangeRequestRow) -> ShipperChangeRequest:
    return ShipperChangeRequest(
        request_id=row.id,
        plan_id=row.plan_id,
        lot_id=row.lot_id,
        shipper_id=row.shipper_id,
        type=row.type,
        new_pallets=row.new_pallets,
        reason=row.reason,
        status=row.status,
        cancellation_estimate=row.estimate,
        alternative_id=row.alternative_id,
        order_id=row.order_id,
        decision_note=row.decision_note,
        created_at=row.created_at,
        updated_at=row.updated_at,
        decided_at=row.decided_at,
    )


def _platform_view(row: ChangeRequestRow, org_name: str) -> PlatformChangeRequest:
    return PlatformChangeRequest(
        **_shipper_view(row).model_dump(),
        shipper_org_id=row.shipper_org_id,
        shipper_org_name=org_name,
        event=to_event(row.type, row.lot_id, row.new_pallets),
        replan_id=row.replan_id,
    )


def _own_lot(access: PlanAccess, lot_id: str, org_id: str) -> Lot:
    session = access.session
    lot = next((x for x in session.scenario.lots if x.id == lot_id), None)
    if lot is None:
        raise HTTPException(status_code=404, detail=LOT_NOT_IN_PLAN)
    if lot.shipper_id not in access.viewer.shipper_ids:
        raise HTTPException(status_code=403, detail=FOREIGN_LOT)
    assigned = next((a for a in session.plan.assignments if a.lot_id == lot_id), None)
    if lot_id not in session.registered_lot_ids or assigned is None:
        raise HTTPException(status_code=409, detail=NOT_PLANNED)
    with database.begin() as db:
        lot_status = db.scalar(
            select(LotRow.status).where(LotRow.id == lot_id, LotRow.shipper_org_id == org_id)
        )
        if lot_status != "planned":
            raise HTTPException(status_code=409, detail=NOT_PLANNED)
        open_id = db.scalar(
            select(ChangeRequestRow.id).where(
                ChangeRequestRow.plan_id == session.plan_id,
                ChangeRequestRow.lot_id == lot_id,
                ChangeRequestRow.status == "open",
            )
        )
        if open_id is not None:
            raise HTTPException(status_code=409, detail=ALREADY_OPEN)
    return lot


def _insert(
    access: PlanAccess,
    principal: Principal,
    lot: Lot,
    body: ChangeRequestCreate,
    estimate: CancellationEstimate | None,
) -> ChangeRequestRow:
    now = store.clock()
    row = ChangeRequestRow(
        id=str(uuid.uuid4()),
        plan_id=access.session.plan_id,
        org_id=access.owner_org_id,
        shipper_org_id=principal.org_id,
        shipper_id=lot.shipper_id,
        lot_id=lot.id,
        type=body.type,
        new_pallets=body.new_pallets,
        reason=body.reason,
        status="open",
        estimate=estimate.model_dump(mode="json") if estimate is not None else None,
        requested_by=principal.user_id,
        created_at=now,
        updated_at=now,
    )
    with database.begin() as db:
        db.add(row)
    return row


@router.post("/shipper/plans/{plan_id}/change-requests", status_code=status.HTTP_201_CREATED)
async def create_change_request(
    plan_id: str, body: ChangeRequestCreate, principal: ShipperPrincipal
) -> ShipperChangeRequest:
    access = await run_in_threadpool(plan_for, plan_id, principal)
    session = access.session
    lot = await run_in_threadpool(_own_lot, access, body.lot_id, principal.org_id)
    assigned = next(a for a in session.plan.assignments if a.lot_id == lot.id)
    if body.type == "air_switch" and assigned.mode == "air":
        raise HTTPException(status_code=409, detail=ALREADY_AIR)
    event = to_event(body.type, lot.id, body.new_pallets)
    apply_event(session.scenario, event)
    estimate = None
    if body.type != "air_switch":
        result = await compute_replan(
            replan,
            session.scenario,
            session.plan,
            event,
            air_locked=session.air_locked_lot_ids,
            deadline=request_deadline(),
        )
        annotate_cancellations(result, session.scenario, session.plan)
        estimate = _estimate(result)
    row = await run_in_threadpool(_insert, access, principal, lot, body, estimate)
    return _shipper_view(row)


@router.get("/shipper/change-requests")
def list_shipper_requests(
    principal: ShipperPrincipal,
    status: ChangeStatus | None = None,
    plan_id: str | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[ShipperChangeRequest]:
    query = select(ChangeRequestRow).where(ChangeRequestRow.shipper_org_id == principal.org_id)
    if status is not None:
        query = query.where(ChangeRequestRow.status == status)
    if plan_id is not None:
        query = query.where(ChangeRequestRow.plan_id == plan_id)
    with database.begin() as db:
        rows = db.scalars(query.order_by(ChangeRequestRow.seq.desc()).limit(limit).offset(offset))
        return [_shipper_view(row) for row in rows]


@router.get("/shipper/notifications")
def list_shipper_notifications(
    principal: ShipperPrincipal,
    plan_id: str | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[Notification]:
    return list_for_org(principal.org_id, plan_id=plan_id, limit=limit, offset=offset)


def _platform_query(principal: Principal):
    return (
        select(ChangeRequestRow, OrganizationRow.name)
        .join(OrganizationRow, OrganizationRow.id == ChangeRequestRow.shipper_org_id)
        .where(ChangeRequestRow.org_id == principal.org_id)
    )


def _platform_request(request_id: str, principal: Principal) -> PlatformChangeRequest:
    with database.begin() as db:
        found = db.execute(
            _platform_query(principal).where(ChangeRequestRow.id == request_id)
        ).first()
    if found is None:
        raise HTTPException(status_code=404, detail=NOT_FOUND)
    row, name = found
    return _platform_view(row, name)


def _open_request(request_id: str, principal: Principal) -> PlatformChangeRequest:
    request = _platform_request(request_id, principal)
    if request.status != "open":
        raise HTTPException(status_code=409, detail=NOT_OPEN)
    return request


def _update(request_id: str, **values) -> bool:
    with database.begin() as db:
        result = db.execute(
            update(ChangeRequestRow)
            .where(ChangeRequestRow.id == request_id, ChangeRequestRow.status == "open")
            .values(updated_at=store.clock(), **values)
        )
        return result.rowcount == 1


def _notice(request: PlatformChangeRequest) -> ChangeRequestNotice:
    return ChangeRequestNotice(
        request_id=request.request_id,
        plan_id=request.plan_id,
        shipper_org_id=request.shipper_org_id,
        shipper_id=request.shipper_id,
        lot_id=request.lot_id,
        type=request.type,
        new_pallets=request.new_pallets,
        order_id=request.order_id,
        note=request.decision_note,
    )


@router.get("/platform/change-requests")
def list_platform_requests(
    principal: PlatformPrincipal,
    status: ChangeStatus | None = None,
    plan_id: str | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[PlatformChangeRequest]:
    query = _platform_query(principal)
    if status is not None:
        query = query.where(ChangeRequestRow.status == status)
    if plan_id is not None:
        query = query.where(ChangeRequestRow.plan_id == plan_id)
    with database.begin() as db:
        rows = db.execute(
            query.order_by(ChangeRequestRow.seq.desc()).limit(limit).offset(offset)
        ).all()
        return [_platform_view(row, name) for row, name in rows]


@router.get("/platform/change-requests/{request_id}")
def read_platform_request(request_id: str, principal: PlatformPrincipal) -> PlatformChangeRequest:
    return _platform_request(request_id, principal)


@router.post("/platform/change-requests/{request_id}/replan")
async def replan_change_request(
    request_id: str,
    principal: PlatformPrincipal,
    llm: Annotated[bool | None, Query()] = None,
) -> ChangeRequestReplan:
    request = await run_in_threadpool(_open_request, request_id, principal)
    response = await post_event(
        request.plan_id, EventRequest(event=request.event), org_id=principal.org_id, llm=llm
    )
    if not await run_in_threadpool(_update, request_id, replan_id=response.replan_id):
        raise HTTPException(status_code=409, detail=NOT_OPEN)
    updated = await run_in_threadpool(_platform_request, request_id, principal)
    return ChangeRequestReplan(change_request=updated, replan=response)


@router.post("/platform/change-requests/{request_id}/apply")
def apply_change_request(
    request_id: str, body: ChangeRequestApply, principal: PlatformPrincipal
) -> ChangeRequestApplied:
    request = _open_request(request_id, principal)
    if request.replan_id is None:
        raise HTTPException(status_code=409, detail=NO_REPLAN)
    session = apply_alternative(
        request.plan_id,
        ApplyRequest(
            alternative_id=body.alternative_id,
            replan_id=request.replan_id,
            idempotency_key=body.idempotency_key,
        ),
        org_id=principal.org_id,
    )
    now = store.clock()
    _update(
        request_id,
        status="applied",
        alternative_id=body.alternative_id,
        order_id=session.active_order_id,
        decision_note=body.note,
        decided_at=now,
    )
    applied = _platform_request(request_id, principal)
    notifications: list[Notification] = []
    if session.active_order_id is not None:
        order = store.order(request.plan_id, session.active_order_id, org_id=principal.org_id)
        notifications += notify_order_confirmation(session, order, principal.org_id, now)
    notifications += notify_change_request_decision(_notice(applied), applied=True, now=now)
    return ChangeRequestApplied(change_request=applied, plan=session, notifications=notifications)


@router.post("/platform/change-requests/{request_id}/reject")
def reject_change_request(
    request_id: str, body: ChangeRequestReject, principal: PlatformPrincipal
) -> PlatformChangeRequest:
    _open_request(request_id, principal)
    now = store.clock()
    if not _update(request_id, status="rejected", decision_note=body.note, decided_at=now):
        raise HTTPException(status_code=409, detail=NOT_OPEN)
    rejected = _platform_request(request_id, principal)
    notify_change_request_decision(_notice(rejected), applied=False, now=now)
    return rejected
