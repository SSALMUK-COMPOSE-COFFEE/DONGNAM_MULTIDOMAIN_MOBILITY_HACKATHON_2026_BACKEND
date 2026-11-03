from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from freshswitch_backend.persistence.database import Database, database
from freshswitch_backend.persistence.tables import DEMO_ORG, OrganizationRow, PlanRow

DEMO_ORG_NAME = "시연"


def insert_demo_org(db: Session) -> None:
    db.execute(
        insert(OrganizationRow)
        .values(id=DEMO_ORG, type="demo", name=DEMO_ORG_NAME)
        .on_conflict_do_nothing(index_elements=[OrganizationRow.id])
    )


def ensure_demo_org(target: Database | None = None) -> None:
    with (target or database).begin() as db:
        insert_demo_org(db)


def sandbox_of(org_id: str) -> Any:
    return select(OrganizationRow.sandbox).where(OrganizationRow.id == org_id).scalar_subquery()


def same_sandbox(column: Any, org_id: str) -> Any:
    return column == sandbox_of(org_id)


def org_sandbox(org_id: str, target: Database | None = None) -> bool:
    with (target or database).begin() as db:
        return bool(db.scalar(select(OrganizationRow.sandbox).where(OrganizationRow.id == org_id)))


def plan_owner_sandboxes(plan_ids: set[str], target: Database | None = None) -> dict[str, bool]:
    if not plan_ids:
        return {}
    with (target or database).begin() as db:
        rows = db.execute(
            select(PlanRow.plan_id, OrganizationRow.sandbox)
            .join(OrganizationRow, OrganizationRow.id == PlanRow.org_id)
            .where(PlanRow.plan_id.in_(plan_ids))
        ).all()
    return {plan_id: sandbox for plan_id, sandbox in rows}
