from fastapi.testclient import TestClient
from test_plan_confirm import REDUCE, _plan
from test_r2_plan_state import _apply, _event, _reject_pohang, _rejected_candidate
from test_r2_tenancy import _go_real

from freshswitch_backend.planning.store import APPLY_BEFORE_CONFIRMATION


def test_real_org_can_drop_rejecting_shipper_while_others_still_confirm(
    platform_client: TestClient, saas_login
) -> None:
    plan_id = _plan(platform_client, saas_login)["plan_id"]
    real = _go_real(saas_login)
    _reject_pohang(real, plan_id)

    other = _event(platform_client, plan_id, REDUCE)
    blocked = platform_client.post(
        f"/api/plans/{plan_id}/apply",
        json={"alternative_id": other["recommended_id"], "replan_id": other["replan_id"]},
    )
    assert blocked.status_code == 409
    assert blocked.json()["detail"] == APPLY_BEFORE_CONFIRMATION

    candidate = _rejected_candidate(platform_client, plan_id)
    applied = _apply(
        platform_client, plan_id, _event(platform_client, plan_id, candidate["events"][0])
    )
    assert all(lot["shipper_id"] != "sh-pohang" for lot in applied["scenario"]["lots"])
