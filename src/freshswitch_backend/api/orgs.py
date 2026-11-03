from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from freshswitch_backend.api.auth import Principal, require_role
from freshswitch_backend.persistence import database
from freshswitch_backend.persistence.orgs import same_sandbox
from freshswitch_backend.persistence.tables import OrganizationRow, PlanForwarderRow, PlanRow

ListedOrgType = Literal["shipper", "forwarder"]
LISTED_TYPES: tuple[str, ...] = ("shipper", "forwarder")

router = APIRouter(prefix="/saas", tags=["saas-orgs"])

PlatformPrincipal = Annotated[Principal, Depends(require_role("platform"))]


class OrgSummary(BaseModel):
    org_id: str
    org_type: str
    org_name: str


class ForwarderRef(BaseModel):
    org_id: str
    org_name: str


class ForwarderAssignRequest(BaseModel):
    forwarder_org_id: str = Field(min_length=1, max_length=64)


class ForwarderAssignment(BaseModel):
    plan_id: str
    forwarder: ForwarderRef


@router.get("/orgs")
def list_orgs(
    principal: PlatformPrincipal,
    org_type: Annotated[ListedOrgType | None, Query(alias="type")] = None,
) -> list[OrgSummary]:
    types = (org_type,) if org_type else LISTED_TYPES
    with database.begin() as db:
        rows = db.execute(
            select(OrganizationRow.id, OrganizationRow.type, OrganizationRow.name)
            .where(
                OrganizationRow.type.in_(types),
                same_sandbox(OrganizationRow.sandbox, principal.org_id),
            )
            .order_by(OrganizationRow.type, OrganizationRow.name, OrganizationRow.id)
        ).all()
    return [OrgSummary(org_id=oid, org_type=kind, org_name=name) for oid, kind, name in rows]


def _lock_own_plan(db: Session, plan_id: str, org_id: str) -> None:
    found = db.scalar(
        select(PlanRow.plan_id)
        .where(PlanRow.plan_id == plan_id, PlanRow.org_id == org_id)
        .with_for_update()
    )
    if found is None:
        raise HTTPException(status_code=404, detail="계획을 찾을 수 없습니다")


@router.put("/platform/plans/{plan_id}/forwarder")
def assign_forwarder(
    plan_id: str, body: ForwarderAssignRequest, principal: PlatformPrincipal
) -> ForwarderAssignment:
    with database.begin() as db:
        _lock_own_plan(db, plan_id, principal.org_id)
        forwarder = db.get(OrganizationRow, body.forwarder_org_id)
        platform = db.get(OrganizationRow, principal.org_id)
        if (
            forwarder is None
            or forwarder.type != "forwarder"
            or platform is None
            or forwarder.sandbox != platform.sandbox
        ):
            raise HTTPException(status_code=422, detail="포워더 조직이 아닙니다")
        db.execute(delete(PlanForwarderRow).where(PlanForwarderRow.plan_id == plan_id))
        db.add(
            PlanForwarderRow(
                plan_id=plan_id,
                forwarder_org_id=forwarder.id,
                assigned_by=principal.user_id,
            )
        )
        return ForwarderAssignment(
            plan_id=plan_id, forwarder=ForwarderRef(org_id=forwarder.id, org_name=forwarder.name)
        )


@router.delete("/platform/plans/{plan_id}/forwarder", status_code=status.HTTP_204_NO_CONTENT)
def unassign_forwarder(plan_id: str, principal: PlatformPrincipal) -> Response:
    with database.begin() as db:
        _lock_own_plan(db, plan_id, principal.org_id)
        db.execute(delete(PlanForwarderRow).where(PlanForwarderRow.plan_id == plan_id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)
