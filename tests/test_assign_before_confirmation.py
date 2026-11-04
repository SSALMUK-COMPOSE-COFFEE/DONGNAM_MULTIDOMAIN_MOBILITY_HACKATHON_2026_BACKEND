from fastapi.testclient import TestClient
from saas_fixtures import FORWARDER_ORG_ID, PLATFORM_ORG_ID
from sqlalchemy import update
from test_saas_api import _platform_plan

from freshswitch_backend.persistence import database
from freshswitch_backend.persistence.tables import OrganizationRow, PlanConfirmationRow, PlanRow


def _assign(client: TestClient, plan_id: str):
    return client.put(
        f"/api/saas/platform/plans/{plan_id}/forwarder",
        json={"forwarder_org_id": FORWARDER_ORG_ID},
    )


def test_sandbox_assignment_before_confirmation_is_allowed_and_flagged(
    platform_client: TestClient, saas_login
) -> None:
    plan = _platform_plan(platform_client, saas_login)
    assert plan["status"] == "pending_shipper_confirmation"
    response = _assign(platform_client, plan["plan_id"])
    assert response.status_code == 200, response.text
    assert response.json()["before_shipper_confirmation"] is True


def test_real_org_assignment_waits_for_shipper_confirmation(
    platform_client: TestClient, saas_login
) -> None:
    plan_id = _platform_plan(platform_client, saas_login)["plan_id"]
    with database.begin() as db:
        db.execute(
            update(OrganizationRow)
            .where(OrganizationRow.id.in_([PLATFORM_ORG_ID, FORWARDER_ORG_ID]))
            .values(sandbox=False, world_id=None)
        )
    blocked = _assign(platform_client, plan_id)
    assert blocked.status_code == 409
    assert blocked.json()["detail"] == "화주 확인이 끝난 뒤 배정할 수 있습니다"
    with database.begin() as db:
        db.execute(update(PlanRow).where(PlanRow.plan_id == plan_id).values(status="committed"))
    assert _assign(platform_client, plan_id).status_code == 409
    with database.begin() as db:
        db.execute(
            update(PlanConfirmationRow)
            .where(PlanConfirmationRow.plan_id == plan_id)
            .values(status="accepted")
        )
    allowed = _assign(platform_client, plan_id)
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["before_shipper_confirmation"] is False
