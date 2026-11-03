from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select

from freshswitch_backend.api.auth import Principal, require_role
from freshswitch_backend.api.saas_access import plan_for, visible_plans
from freshswitch_backend.api.saas_views import (
    ForwarderOrder,
    PlanListItem,
    forwarder_order_view,
    forwarders_of,
    plan_item,
    without_settlement,
)
from freshswitch_backend.execution.models import ConfirmRequest
from freshswitch_backend.persistence import database
from freshswitch_backend.persistence.tables import PlanRow
from freshswitch_backend.planning.store import (
    OrderInputError,
    PlanSession,
    SessionConflict,
    store,
)

router = APIRouter(prefix="/saas/forwarder", tags=["saas-forwarder"])

ForwarderPrincipal = Annotated[Principal, Depends(require_role("forwarder"))]


class ForwarderDecision(BaseModel):
    accept: bool
    note: str | None = None


@router.get("/plans")
def list_plans(
    principal: ForwarderPrincipal,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[PlanListItem]:
    with database.begin() as db:
        rows = db.scalars(
            select(PlanRow)
            .where(visible_plans(principal))
            .order_by(PlanRow.created_at.desc(), PlanRow.seq.desc())
            .limit(limit)
            .offset(offset)
        )
        sessions = [PlanSession.model_validate(row.payload) for row in rows]
    forwarders = forwarders_of([s.plan_id for s in sessions])
    return [plan_item(s, forwarders.get(s.plan_id)) for s in sessions]


@router.get("/plans/{plan_id}")
def read_plan(plan_id: str, principal: ForwarderPrincipal) -> dict[str, Any]:
    return without_settlement(plan_for(plan_id, principal).session)


@router.get("/plans/{plan_id}/orders")
def list_orders(plan_id: str, principal: ForwarderPrincipal) -> list[ForwarderOrder]:
    access = plan_for(plan_id, principal)
    return [forwarder_order_view(o) for o in store.orders(plan_id, org_id=access.owner_org_id)]


@router.post("/plans/{plan_id}/orders/{order_id}/confirm")
def confirm_order(
    plan_id: str, order_id: str, body: ForwarderDecision, principal: ForwarderPrincipal
) -> ForwarderOrder:
    access = plan_for(plan_id, principal)
    request = ConfirmRequest(actor="forwarder", accept=body.accept, note=body.note)
    try:
        order = store.confirm(plan_id, order_id, request, org_id=access.owner_org_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="실행 지시를 찾을 수 없습니다") from exc
    except OrderInputError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except SessionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return forwarder_order_view(order)
