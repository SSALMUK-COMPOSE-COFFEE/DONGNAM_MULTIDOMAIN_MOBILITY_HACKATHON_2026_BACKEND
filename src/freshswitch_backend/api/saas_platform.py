from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from freshswitch_backend.api.auth import Principal, require_role
from freshswitch_backend.api.plans import CreatePlanRequest, create_tenant_plan
from freshswitch_backend.api.saas_views import (
    PlanListItem,
    PlatformSettlement,
    forwarders_of,
    plan_item,
    platform_settlement,
)
from freshswitch_backend.intake.tenants import load_catalog
from freshswitch_backend.lots import ANY_ORG, LotStatus, RegisteredLot
from freshswitch_backend.persistence import database
from freshswitch_backend.persistence.tables import LotRow, OrganizationRow, PlanRow
from freshswitch_backend.planning.store import PlanSession

router = APIRouter(prefix="/saas/platform", tags=["saas-platform"])

PlatformPrincipal = Annotated[Principal, Depends(require_role("platform"))]


class PlatformLot(RegisteredLot):
    shipper_org_id: str
    shipper_org_name: str


class PlatformPlanRequest(BaseModel):
    scenario_id: str = "default"
    use_learned_loss: bool = False
    lot_ids: list[str] = Field(default_factory=list)
    include_registered: bool = False


@router.get("/lots")
def list_lots(
    principal: PlatformPrincipal,
    status: LotStatus | None = None,
    scenario_id: str | None = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[PlatformLot]:
    query = (
        select(LotRow, OrganizationRow.name)
        .join(OrganizationRow, OrganizationRow.id == LotRow.shipper_org_id)
        .where(OrganizationRow.type == "shipper")
        .order_by(LotRow.created_at.desc(), LotRow.id)
        .limit(limit)
        .offset(offset)
    )
    if status is not None:
        query = query.where(LotRow.status == status)
    if scenario_id is not None:
        query = query.where(LotRow.scenario_id == scenario_id)
    with database.begin() as db:
        return [
            PlatformLot(
                **RegisteredLot.model_validate(row, from_attributes=True).model_dump(),
                shipper_org_id=row.shipper_org_id,
                shipper_org_name=name,
            )
            for row, name in db.execute(query).all()
        ]


@router.post("/plans", status_code=status.HTTP_201_CREATED)
def create_plan(body: PlatformPlanRequest, principal: PlatformPrincipal) -> PlanSession:
    request = CreatePlanRequest(**body.model_dump())
    return create_tenant_plan(request, principal.org_id)


def _sessions(principal: Principal, limit: int, offset: int) -> list[PlanSession]:
    with database.begin() as db:
        rows = db.scalars(
            select(PlanRow)
            .where(PlanRow.org_id == principal.org_id)
            .order_by(PlanRow.created_at.desc(), PlanRow.seq.desc())
            .limit(limit)
            .offset(offset)
        )
        return [PlanSession.model_validate(row.payload) for row in rows]


@router.get("/plans")
def list_plans(
    principal: PlatformPrincipal,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[PlanListItem]:
    sessions = _sessions(principal, limit, offset)
    forwarders = forwarders_of([s.plan_id for s in sessions])
    return [plan_item(s, forwarders.get(s.plan_id)) for s in sessions]


@router.get("/settlements")
def list_settlements(
    principal: PlatformPrincipal,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[PlatformSettlement]:
    names = load_catalog(ANY_ORG).org_names()
    return [platform_settlement(s, names) for s in _sessions(principal, limit, offset)]
