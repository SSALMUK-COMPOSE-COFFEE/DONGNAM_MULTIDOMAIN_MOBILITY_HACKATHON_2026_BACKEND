import json
import re

from fastapi.testclient import TestClient
from saas_fixtures import FORWARDER_ORG_ID

from freshswitch_backend.data.loader import get_scenario
from freshswitch_backend.persistence import database
from freshswitch_backend.persistence.seed import shipper_org_id
from freshswitch_backend.persistence.tables import PlanForwarderRow

POHANG_REDUCE = {"type": "reduce", "lot_id": "lot-pohang-gr-1", "new_pallets": 2}
MONEY = re.compile(r"\d[\d,]*(?:\.\d+)?\s*(?:만\s*)?원")


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


def _reduced_plan(platform_client: TestClient, saas_login) -> tuple[str, str]:
    scenario = get_scenario("default")
    assert scenario is not None
    ids = []
    for lot in scenario.lots:
        client = saas_login(shipper_org_id(lot.shipper_id))
        response = client.post("/api/saas/shipper/lots", json=_lot_body(lot))
        assert response.status_code == 201, response.text
        ids.append(response.json()["registered"]["id"])
    created = platform_client.post("/api/saas/platform/plans", json={"lot_ids": ids})
    assert created.status_code == 201, created.text
    plan_id = created.json()["plan_id"]
    with database.begin() as db:
        db.add(PlanForwarderRow(plan_id=plan_id, forwarder_org_id=FORWARDER_ORG_ID))
    event = platform_client.post(f"/api/plans/{plan_id}/events", json={"event": POHANG_REDUCE})
    assert event.status_code == 200, event.text
    applied = platform_client.post(
        f"/api/plans/{plan_id}/apply", json={"alternative_id": "pull_forward"}
    )
    assert applied.status_code == 200, applied.text
    return plan_id, applied.json()["active_order_id"]


def _strings(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [s for item in value for s in _strings(item)]
    if isinstance(value, dict):
        return [s for item in value.values() for s in _strings(item)]
    return []


def _money_texts(payload) -> list[str]:
    return [s for s in _strings(payload) if MONEY.search(s)]


def _settlement_texts(orders: list[dict]) -> list[str]:
    return [
        action["description"]
        for order in orders
        for action in order["automatic"]
        if action["kind"] == "settlement_recompute"
    ]


def test_forwarder_free_text_has_no_settlement_amounts(
    platform_client: TestClient, saas_login, forwarder_client: TestClient
) -> None:
    plan_id, _ = _reduced_plan(platform_client, saas_login)
    platform_orders = platform_client.get(f"/api/plans/{plan_id}/orders").json()
    assert any(MONEY.search(t) for t in _settlement_texts(platform_orders))

    orders = forwarder_client.get(f"/api/saas/forwarder/plans/{plan_id}/orders").json()
    texts = _settlement_texts(orders)
    assert texts and all(not MONEY.search(t) for t in texts)
    assert _money_texts([o["automatic"] for o in orders]) == []

    plan = forwarder_client.get(f"/api/saas/forwarder/plans/{plan_id}").json()
    explanation = plan["history"][0]["explanation"]
    assert explanation["shipper_messages"] == []
    assert not MONEY.search(explanation["operator_summary"])
    assert "감량" in explanation["operator_summary"]

    platform_plan = platform_client.get(f"/api/plans/{plan_id}").json()
    assert platform_plan["history"][0]["explanation"]["shipper_messages"]


def test_shipper_free_text_has_only_own_amounts(platform_client: TestClient, saas_login) -> None:
    plan_id, _ = _reduced_plan(platform_client, saas_login)
    platform_plan = platform_client.get(f"/api/plans/{plan_id}").json()
    platform_messages = platform_plan["history"][0]["explanation"]["shipper_messages"]
    sangju_message = next(m for m in platform_messages if m["shipper_id"] == "sh-sangju")
    sangju_amounts = MONEY.findall(sangju_message["message"])
    assert sangju_amounts

    pohang = saas_login("shipper-pohang")
    plan = pohang.get(f"/api/saas/shipper/plans/{plan_id}").json()
    explanation = plan["history"][0]["explanation"]
    assert [m["shipper_id"] for m in explanation["shipper_messages"]] == ["sh-pohang"]
    assert not MONEY.search(explanation["operator_summary"])
    text = json.dumps(plan, ensure_ascii=False)
    for amount in sangju_amounts:
        assert amount not in text

    orders = pohang.get(f"/api/saas/shipper/plans/{plan_id}/orders").json()
    assert orders
    for description in _settlement_texts(orders):
        assert "플랫폼 성과보수" not in description
        assert "화주 총비용" not in description
    for order in orders:
        cancellation = order["cancellation"]
        if cancellation is None:
            continue
        assert all(t["shipper_id"] == "sh-pohang" for t in cancellation["transfers"])
        receivers = [t["amount"] for t in cancellation["transfers"]]
        for description in _settlement_texts([order]):
            amounts = {int(a.replace(",", "")) for a in re.findall(r"([\d,]+)원", description)}
            assert amounts <= {cancellation["fee"], *receivers}

    sangju = saas_login("shipper-sangju")
    for order in sangju.get(f"/api/saas/shipper/plans/{plan_id}/orders").json():
        cancellation = order["cancellation"]
        if cancellation is not None:
            assert cancellation["fee"] is None
            assert all(t["shipper_id"] == "sh-sangju" for t in cancellation["transfers"])
