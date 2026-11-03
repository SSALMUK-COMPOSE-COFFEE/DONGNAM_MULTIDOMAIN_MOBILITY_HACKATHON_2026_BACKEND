from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import select

from freshswitch_backend.api.auth import Principal, require_role
from freshswitch_backend.api.intake import IntakeScope, register
from freshswitch_backend.api.lots import delete_unlocked_lot, update_locked_lot
from freshswitch_backend.api.saas_access import (
    PlanAccess,
    org_shipper_ids,
    plan_for,
    visible_plans,
)
from freshswitch_backend.api.saas_views import (
    ShipperPlanItem,
    ShipperSettlementRow,
    anonymized_orders,
    anonymized_session,
    shipper_lot_ids,
    shipper_order_view,
    shipper_plan_item,
    shipper_settlements,
)
from freshswitch_backend.execution.models import ConfirmRequest, RequestResultBody
from freshswitch_backend.intake.models import IntakePreview, IntakeRequest
from freshswitch_backend.lots import LotFilter, LotPatch, LotStatus, RegisteredLot, lot_store
from freshswitch_backend.persistence import database
from freshswitch_backend.persistence.tables import PlanRow
from freshswitch_backend.planning.store import (
    OrderInputError,
    PlanSession,
    SessionConflict,
    store,
)

router = APIRouter(prefix="/saas/shipper", tags=["saas-shipper"])

ShipperPrincipal = Annotated[Principal, Depends(require_role("shipper"))]


class ShipperLotCreate(IntakeRequest):
    shipper: str | None = None
    shipper_org_id: str | None = None


class ShipperDecision(BaseModel):
    accept: bool
    note: str | None = None
    shipper_id: str | None = None


def _scope(principal: Principal) -> IntakeScope:
    return IntakeScope(
        org_id=principal.org_id, catalog_org=principal.org_id, lot_org=principal.org_id
    )


def _own_shipper(principal: Principal, requested: str | None) -> str:
    ids = org_shipper_ids(principal.org_id)
    if not ids:
        raise HTTPException(status_code=422, detail="조직에 등록된 화주 정보가 없습니다")
    return requested if requested in ids else ids[0]


def _access(plan_id: str, principal: Principal) -> PlanAccess:
    return plan_for(plan_id, principal)


@router.get("/lots")
def list_lots(
    principal: ShipperPrincipal,
    status: LotStatus | None = None,
    ready_from: date | None = None,
    ready_to: date | None = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[RegisteredLot]:
    return lot_store.search(
        LotFilter(
            status=status, ready_from=ready_from, ready_to=ready_to, limit=limit, offset=offset
        ),
        org_id=principal.org_id,
    )


@router.post("/lots", status_code=status.HTTP_201_CREATED)
def create_lot(body: ShipperLotCreate, principal: ShipperPrincipal) -> IntakePreview:
    fields = body.model_dump(exclude={"shipper", "shipper_org_id", "plan_id", "save"})
    owned = IntakeRequest(
        **fields, shipper=_own_shipper(principal, body.shipper), plan_id=None, save=True
    )
    return register(owned, _scope(principal))


@router.patch("/lots/{lot_id}")
def update_lot(lot_id: str, patch: LotPatch, principal: ShipperPrincipal) -> RegisteredLot:
    fields = patch.model_fields_set - {"shipper"}
    cleaned = LotPatch(**{name: getattr(patch, name) for name in fields})
    return update_locked_lot(lot_id, cleaned, _scope(principal), principal.org_id)


@router.delete("/lots/{lot_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_lot(lot_id: str, principal: ShipperPrincipal) -> None:
    delete_unlocked_lot(lot_id, principal.org_id)


def _sessions(principal: Principal) -> list[PlanSession]:
    with database.begin() as db:
        rows = db.scalars(
            select(PlanRow)
            .where(visible_plans(principal))
            .order_by(PlanRow.created_at.desc(), PlanRow.seq.desc())
        )
        return [PlanSession.model_validate(row.payload) for row in rows]


@router.get("/plans")
def list_plans(principal: ShipperPrincipal) -> list[ShipperPlanItem]:
    mine = lot_store.ids(org_id=principal.org_id)
    return [shipper_plan_item(session, mine) for session in _sessions(principal)]


@router.get("/plans/{plan_id}")
def read_plan(plan_id: str, principal: ShipperPrincipal) -> dict[str, Any]:
    access = _access(plan_id, principal)
    return anonymized_session(access.session, access.viewer.shipper_ids)


@router.get("/plans/{plan_id}/orders")
def list_orders(plan_id: str, principal: ShipperPrincipal) -> list[dict[str, Any]]:
    access = _access(plan_id, principal)
    orders = store.orders(plan_id, org_id=access.owner_org_id)
    return anonymized_orders(access.session, orders, access.viewer.shipper_ids)


@router.post("/plans/{plan_id}/orders/{order_id}/requests/{request_id}")
def answer_request(
    plan_id: str,
    order_id: str,
    request_id: str,
    body: RequestResultBody,
    principal: ShipperPrincipal,
) -> dict[str, Any]:
    access = _access(plan_id, principal)
    owner = access.owner_org_id
    missing = HTTPException(status_code=404, detail="실행 지시 또는 요청을 찾을 수 없습니다")
    try:
        order = store.order(plan_id, order_id, org_id=owner)
    except KeyError as exc:
        raise missing from exc
    my_lots = shipper_lot_ids(access.session, access.viewer.shipper_ids)
    request = next((r for r in order.requests if r.request_id == request_id), None)
    if request is None or not my_lots & set(request.lot_ids):
        raise missing
    try:
        updated = store.answer_request(plan_id, order_id, request_id, body, org_id=owner)
    except KeyError as exc:
        raise missing from exc
    except SessionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return shipper_order_view(access.session, updated, access.viewer.shipper_ids)


@router.post("/plans/{plan_id}/orders/{order_id}/confirm")
def confirm_order(
    plan_id: str, order_id: str, body: ShipperDecision, principal: ShipperPrincipal
) -> dict[str, Any]:
    access = _access(plan_id, principal)
    owner = access.owner_org_id
    missing = HTTPException(status_code=404, detail="실행 지시를 찾을 수 없습니다")
    try:
        order = store.order(plan_id, order_id, org_id=owner)
    except KeyError as exc:
        raise missing from exc
    own = access.viewer.shipper_ids
    pending = [c.shipper_id for c in order.shipper_confirmations if c.shipper_id in own]
    if not pending:
        raise missing
    shipper_id = body.shipper_id if body.shipper_id in pending else pending[0]
    request = ConfirmRequest(
        actor="shipper", shipper_id=shipper_id, accept=body.accept, note=body.note
    )
    try:
        updated = store.confirm(plan_id, order_id, request, org_id=owner)
    except KeyError as exc:
        raise missing from exc
    except OrderInputError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except SessionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return shipper_order_view(access.session, updated, own)


@router.get("/settlements")
def list_settlements(principal: ShipperPrincipal) -> list[ShipperSettlementRow]:
    own = org_shipper_ids(principal.org_id)
    return [row for session in _sessions(principal) for row in shipper_settlements(session, own)]
