from fastapi.testclient import TestClient
from saas_fixtures import (
    FORWARDER_ORG_ID,
    REAL_PLATFORM_EMAIL,
    REAL_PLATFORM_PASSWORD,
    create_real_platform,
    password_client,
)

from freshswitch_backend.data.loader import get_scenario
from freshswitch_backend.main import app
from freshswitch_backend.notifications import MOCK_CHANNEL, notify_plan_confirmation
from freshswitch_backend.persistence import database
from freshswitch_backend.persistence.seed import shipper_org_id
from freshswitch_backend.persistence.tables import DEMO_ORG, PlanForwarderRow
from freshswitch_backend.planning.store import store

POHANG_LOT = "lot-pohang-gr-1"
SANGJU_LOT = "lot-sangju-gr-1"
REDUCE = {"type": "reduce", "lot_id": POHANG_LOT, "new_pallets": 2, "reason": "작황 부진"}


def _lot_body(lot) -> dict:
    return {
        "product": lot.product_id,
        "pallets": lot.pallets,
        "ready_date": lot.ready_date.isoformat(),
        "destination": lot.destination,
        "sell_by_date": lot.sell_by_date.isoformat(),
        "sell_window": lot.sell_window,
        "lot_id": lot.id,
    }


def _plan(platform_client: TestClient, saas_login, skip: tuple[str, ...] = ()) -> str:
    scenario = get_scenario("default")
    assert scenario is not None
    ids = []
    for lot in scenario.lots:
        if lot.id in skip:
            continue
        client = saas_login(shipper_org_id(lot.shipper_id))
        response = client.post("/api/saas/shipper/lots", json=_lot_body(lot))
        assert response.status_code == 201, response.text
        ids.append(response.json()["registered"]["id"])
    created = platform_client.post("/api/saas/platform/plans", json={"lot_ids": ids})
    assert created.status_code == 201, created.text
    plan_id = created.json()["plan_id"]
    with database.begin() as db:
        db.add(PlanForwarderRow(plan_id=plan_id, forwarder_org_id=FORWARDER_ORG_ID))
    return plan_id


def _request(client: TestClient, plan_id: str, body: dict = REDUCE):
    return client.post(f"/api/saas/shipper/plans/{plan_id}/change-requests", json=body)


def test_change_request_flow_replan_apply_and_notify(
    platform_client: TestClient, saas_login, forwarder_client: TestClient
) -> None:
    plan_id = _plan(platform_client, saas_login)
    pohang = saas_login("shipper-pohang")
    sangju = saas_login("shipper-sangju")

    created = _request(pohang, plan_id)
    assert created.status_code == 201, created.text
    request = created.json()
    assert request["status"] == "open"
    assert request["shipper_id"] == "sh-pohang"
    assert request["new_pallets"] == 2
    estimate = request["cancellation_estimate"]
    assert estimate is not None
    assert estimate["cancelled_pallets"] == 3
    assert estimate["min_fee"] <= estimate["fee"] <= estimate["max_fee"]
    assert "transfers" not in estimate
    request_id = request["request_id"]

    mine = pohang.get("/api/saas/shipper/change-requests").json()
    assert [r["request_id"] for r in mine] == [request_id]
    assert sangju.get("/api/saas/shipper/change-requests").json() == []

    listed = platform_client.get("/api/saas/platform/change-requests?status=open").json()
    assert [r["request_id"] for r in listed] == [request_id]
    assert listed[0]["event"] == {"type": "reduce", "lot_id": POHANG_LOT, "new_pallets": 2}
    assert listed[0]["shipper_org_id"] == "shipper-pohang"
    assert listed[0]["shipper_org_name"]

    early = platform_client.post(
        f"/api/saas/platform/change-requests/{request_id}/apply",
        json={"alternative_id": "pull_forward"},
    )
    assert early.status_code == 409

    computed = platform_client.post(f"/api/saas/platform/change-requests/{request_id}/replan")
    assert computed.status_code == 200, computed.text
    replan = computed.json()["replan"]
    assert {a["id"] for a in replan["alternatives"]} == {
        "pull_forward",
        "next_voyage",
        "sail_as_is",
    }
    assert replan["event"]["lot_id"] == POHANG_LOT
    assert computed.json()["change_request"]["replan_id"] == replan["replan_id"]

    applied = platform_client.post(
        f"/api/saas/platform/change-requests/{request_id}/apply",
        json={"alternative_id": "pull_forward", "note": "감량 반영"},
    )
    assert applied.status_code == 200, applied.text
    body = applied.json()
    order_id = body["plan"]["active_order_id"]
    assert order_id
    assert body["plan"]["status"] == "pending_confirmation"
    assert body["change_request"]["status"] == "applied"
    assert body["change_request"]["order_id"] == order_id
    assert body["change_request"]["alternative_id"] == "pull_forward"
    assert body["change_request"]["decided_at"] is not None
    lot = next(x for x in body["plan"]["scenario"]["lots"] if x["id"] == POHANG_LOT)
    assert lot["pallets"] == 2

    again = platform_client.post(
        f"/api/saas/platform/change-requests/{request_id}/apply",
        json={"alternative_id": "pull_forward"},
    )
    assert again.status_code == 409
    assert pohang.get("/api/saas/shipper/change-requests").json()[0]["status"] == "applied"

    order = platform_client.get(f"/api/plans/{plan_id}/orders/{order_id}").json()
    confirming = {c["shipper_id"] for c in order["shipper_confirmations"]}
    assert "sh-pohang" in confirming
    notified = pohang.get("/api/saas/shipper/notifications").json()
    kinds = {n["kind"] for n in notified}
    assert {"change_request_applied", "order_confirmation_requested"} <= kinds
    for item in notified:
        assert item["channel"] == MOCK_CHANNEL
        assert item["sent"] is False
        assert item["shipper_id"] == "sh-pohang"
        assert plan_id in item["link"]
    applied_note = next(n for n in notified if n["kind"] == "change_request_applied")
    assert applied_note["change_request_id"] == request_id
    assert POHANG_LOT in applied_note["body"]

    for shipper_id in confirming - {"sh-pohang"}:
        other = saas_login(shipper_org_id(shipper_id))
        items = other.get("/api/saas/shipper/notifications").json()
        assert [n["kind"] for n in items] == ["order_confirmation_requested"]
        assert all(n["shipper_id"] == shipper_id for n in items)
        assert POHANG_LOT not in items[0]["body"]

    for shipper_id in confirming:
        shipper = saas_login(shipper_org_id(shipper_id))
        answered = shipper.post(
            f"/api/saas/shipper/plans/{plan_id}/orders/{order_id}/confirm",
            json={"accept": True, "shipper_id": shipper_id},
        )
        assert answered.status_code == 200, answered.text
    for _ in range(2):
        done = forwarder_client.post(
            f"/api/saas/forwarder/plans/{plan_id}/orders/{order_id}/confirm",
            json={"accept": True},
        )
        assert done.status_code == 200, done.text
    committed = [
        n
        for n in pohang.get("/api/saas/shipper/notifications").json()
        if n["kind"] == "order_committed"
    ]
    assert len(committed) == 1
    assert committed[0]["order_id"] == order_id


def test_shipper_cannot_request_change_on_other_shipper_lot(
    platform_client: TestClient, saas_login
) -> None:
    plan_id = _plan(platform_client, saas_login)
    sangju = saas_login("shipper-sangju")

    foreign = _request(sangju, plan_id)
    assert foreign.status_code == 403
    missing = _request(sangju, plan_id, {"type": "cancel", "lot_id": "lot-none"})
    assert missing.status_code == 404
    assert sangju.get("/api/saas/shipper/change-requests").json() == []
    assert platform_client.get("/api/saas/platform/change-requests").json() == []

    no_pallets = _request(
        saas_login("shipper-pohang"), plan_id, {"type": "reduce", "lot_id": POHANG_LOT}
    )
    assert no_pallets.status_code == 422
    too_many = _request(
        saas_login("shipper-pohang"),
        plan_id,
        {"type": "reduce", "lot_id": POHANG_LOT, "new_pallets": 5},
    )
    assert too_many.status_code == 422


def test_shipper_outside_plan_gets_404(platform_client: TestClient, saas_login) -> None:
    plan_id = _plan(platform_client, saas_login, skip=(POHANG_LOT,))
    pohang = saas_login("shipper-pohang")
    assert _request(pohang, plan_id).status_code == 404


def test_reject_records_notification_and_blocks_duplicates(
    platform_client: TestClient, saas_login
) -> None:
    plan_id = _plan(platform_client, saas_login)
    sangju = saas_login("shipper-sangju")
    body = {"type": "cancel", "lot_id": SANGJU_LOT}
    created = _request(sangju, plan_id, body)
    assert created.status_code == 201, created.text
    request_id = created.json()["request_id"]
    assert _request(sangju, plan_id, body).status_code == 409

    rejected = platform_client.post(
        f"/api/saas/platform/change-requests/{request_id}/reject", json={"note": "선적 마감 지남"}
    )
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["status"] == "rejected"
    assert rejected.json()["decision_note"] == "선적 마감 지남"
    assert (
        platform_client.post(
            f"/api/saas/platform/change-requests/{request_id}/reject", json={}
        ).status_code
        == 409
    )
    assert (
        platform_client.post(f"/api/saas/platform/change-requests/{request_id}/replan").status_code
        == 409
    )
    items = sangju.get("/api/saas/shipper/notifications").json()
    assert [n["kind"] for n in items] == ["change_request_rejected"]
    assert "선적 마감 지남" in items[0]["body"]
    assert items[0]["channel"] == MOCK_CHANNEL

    assert _request(sangju, plan_id, body).status_code == 201


def test_other_platform_and_roles_cannot_see_requests(
    platform_client: TestClient, saas_login, forwarder_client: TestClient
) -> None:
    plan_id = _plan(platform_client, saas_login)
    created = _request(saas_login("shipper-pohang"), plan_id)
    assert created.status_code == 201, created.text
    request_id = created.json()["request_id"]

    create_real_platform()
    other = password_client(app, REAL_PLATFORM_EMAIL, REAL_PLATFORM_PASSWORD)
    assert other.get("/api/saas/platform/change-requests").json() == []
    assert other.get(f"/api/saas/platform/change-requests/{request_id}").status_code == 404
    assert other.post(f"/api/saas/platform/change-requests/{request_id}/replan").status_code == 404
    assert (
        other.post(
            f"/api/saas/platform/change-requests/{request_id}/apply",
            json={"alternative_id": "pull_forward"},
        ).status_code
        == 404
    )
    assert (
        other.post(f"/api/saas/platform/change-requests/{request_id}/reject", json={}).status_code
        == 404
    )
    assert forwarder_client.get("/api/saas/platform/change-requests").status_code == 403
    assert forwarder_client.get("/api/saas/shipper/notifications").status_code == 403
    assert (
        platform_client.post(f"/api/saas/shipper/plans/{plan_id}/change-requests", json=REDUCE)
    ).status_code == 403
    assert len(platform_client.get("/api/saas/platform/change-requests").json()) == 1


def test_plan_confirmation_hook_records_per_shipper(
    platform_client: TestClient, saas_login
) -> None:
    plan_id = _plan(platform_client, saas_login)
    owner = store.org_of(plan_id)
    session = store.get(plan_id, org_id=owner)
    created = notify_plan_confirmation(session, owner, store.clock())
    assert {n.shipper_id for n in created} == {a.shipper_id for a in session.plan.assignments}
    assert notify_plan_confirmation(session, owner, store.clock()) == []
    items = saas_login("shipper-jinju").get("/api/saas/shipper/notifications").json()
    assert [n["kind"] for n in items] == ["plan_confirmation_requested"]
    assert items[0]["shipper_id"] == "sh-jinju"
    assert notify_plan_confirmation(session, DEMO_ORG, store.clock()) == []
