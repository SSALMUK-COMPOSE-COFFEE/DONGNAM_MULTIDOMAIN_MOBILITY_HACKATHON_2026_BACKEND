from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from freshswitch_backend.persistence.database import Database, database
from freshswitch_backend.persistence.tables import DEMO_ORG, OrganizationRow

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
