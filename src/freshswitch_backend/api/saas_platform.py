from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from freshswitch_backend.api.auth import Principal, require_role
from freshswitch_backend.api.compute_limits import (
    cache_key,
    check_lot_count,
    check_preview_rate,
    preview_cache,
    preview_deadline,
    run_bounded,
)
from freshswitch_backend.api.plans import (
    CreatePlanRequest,
    create_tenant_plan,
    tenant_plan_scenario,
)
from freshswitch_backend.api.saas_views import (
    PlanListItem,
    PlatformSettlement,
    forwarders_of,
    plan_item,
    platform_settlement,
)
from freshswitch_backend.domain.models import Plan, Scenario
from freshswitch_backend.intake.tenants import load_catalog
from freshswitch_backend.lots import ANY_ORG, LotStatus, RegisteredLot
from freshswitch_backend.persistence import database
from freshswitch_backend.persistence.orgs import same_sandbox
from freshswitch_backend.persistence.tables import LotRow, OrganizationRow, PlanRow
from freshswitch_backend.planning.service import VoyagePickup, build_plan
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


class PlanPreview(BaseModel):
    scenario: Scenario
    plan: Plan
    routes: list[VoyagePickup]


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
        .where(
            OrganizationRow.type == "shipper",
            same_sandbox(OrganizationRow.sandbox, principal.org_id),
        )
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


def _compute_preview(scenario: Scenario, deadline: float) -> PlanPreview:
    working = scenario.model_copy(deep=True)
    built = build_plan(working, deadline=deadline)
    return PlanPreview(scenario=working, plan=built.plan, routes=built.routes)


@router.post("/plans/preview")
def preview_plan(
    body: PlatformPlanRequest, request: Request, principal: PlatformPrincipal
) -> PlanPreview:
    deadline = preview_deadline()
    check_lot_count(len(dict.fromkeys(body.lot_ids)))
    scenario, registered_ids = tenant_plan_scenario(
        CreatePlanRequest(**body.model_dump()), principal.org_id
    )
    key = cache_key(
        {
            "org_id": principal.org_id,
            "lot_ids": sorted(registered_ids),
            "options": body.model_dump(exclude={"lot_ids"}),
            "scenario": scenario.model_dump(mode="json"),
        }
    )
    cache = preview_cache(request)
    cached = cache.get(key)
    if cached is not None:
        return cached
    check_preview_rate(request, principal)
    preview = run_bounded(_compute_preview, scenario, deadline=deadline)
    cache.put(key, preview)
    return preview


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
    names = load_catalog(ANY_ORG, peer_of=principal.org_id).org_names()
    return [platform_settlement(s, names) for s in _sessions(principal, limit, offset)]
