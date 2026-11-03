import json
from datetime import datetime

from fastapi.testclient import TestClient
from saas_fixtures import FORWARDER_ORG_ID
from test_saas_api import REDUCE, _assign, _platform_plan

from freshswitch_backend.data.loader import get_scenario
from freshswitch_backend.domain.models import KST, Scenario
from freshswitch_backend.execution.card import EnteredNumbers, mock_booking_no
from freshswitch_backend.execution.orders import saas_requests
from freshswitch_backend.planning.store import store

DELAY = {
    "type": "delay",
    "voyage_id": "V-2611A",
    "new_etd": "2026-11-20T18:00:00+09:00",
    "new_cy_cutoff": "2026-11-19T18:00:00+09:00",
}


def _default() -> Scenario:
    scenario = get_scenario("default")
    assert scenario is not None
    return scenario


def _delayed(scenario: Scenario) -> Scenario:
    voyages = [
        v.model_copy(
            update={
                "etd": datetime(2026, 11, 20, 18, 0, tzinfo=KST),
                "cy_cutoff": datetime(2026, 11, 19, 18, 0, tzinfo=KST),
            }
        )
        if v.id == "V-2611A"
        else v
        for v in scenario.voyages
    ]
    return scenario.model_copy(update={"voyages": voyages})


def _apply(client: TestClient, plan_id: str, event: dict, alternative: str | None = None) -> None:
    sent = client.post(f"/api/plans/{plan_id}/events", json={"event": event})
    assert sent.status_code == 200, sent.text
    body = sent.json()
    chosen = alternative or body["recommended_id"]
    applied = client.post(f"/api/plans/{plan_id}/apply", json={"alternative_id": chosen})
    assert applied.status_code == 200, applied.text


def _booking(order, kind: str):
    return next(r for r in order.requests if r.kind == kind)


def test_engine4_old_order_text_keeps_creation_schedule(client: TestClient) -> None:
    plan_id = store.create(_default()).plan_id
    _apply(client, plan_id, REDUCE, "pull_forward")
    order = store.orders(plan_id)[-1]
    original = saas_requests(plan_id, order.requests, _default())
    later = saas_requests(plan_id, order.requests, _delayed(_default()))
    text = next(r.text for r in later if r.kind == "booking_amendment")
    assert "ETD 11/18 18:00" in text
    assert "ETD 11/20" not in text
    assert "CY 반입 마감 11/19 18:00" not in text
    assert [r.text for r in later] == [r.text for r in original]


def test_engine4_saas_views_keep_text_after_later_event(
    platform_client: TestClient, saas_login, forwarder_client: TestClient
) -> None:
    plan_id = _platform_plan(platform_client, saas_login)["plan_id"]
    _assign(plan_id, FORWARDER_ORG_ID)
    _apply(platform_client, plan_id, REDUCE, "pull_forward")
    first = platform_client.get(f"/api/plans/{plan_id}/orders").json()[-1]
    before = next(r for r in first["requests"] if r["kind"] == "booking_amendment")
    assert "ETD 11/18 18:00" in before["text"]
    _apply(platform_client, plan_id, DELAY)
    orders = platform_client.get(f"/api/plans/{plan_id}/orders").json()
    same = next(o for o in orders if o["order_id"] == first["order_id"])
    after = next(r for r in same["requests"] if r["request_id"] == before["request_id"])
    assert after["text"] == before["text"]
    assert "saas_basis" not in json.dumps(orders)
    sent = forwarder_client.get(f"/api/saas/forwarder/plans/{plan_id}/orders").json()
    forwarded = next(o for o in sent if o["order_id"] == first["order_id"])
    text = next(r["text"] for r in forwarded["requests"] if r["request_id"] == before["request_id"])
    assert text == before["text"]


def test_engine4_demo_orders_have_no_snapshot_key(client: TestClient) -> None:
    plan_id = client.post("/api/plans", json={"scenario_id": "default"}).json()["plan_id"]
    _apply(client, plan_id, REDUCE, "pull_forward")
    orders = client.get(f"/api/plans/{plan_id}/orders").json()
    assert "saas_basis" not in json.dumps(orders)
    assert orders[-1]["requests"][0]["text"].startswith("[부킹 변경 요청]")


def test_engine5_cancelled_lot_weighs_product_net_kg(client: TestClient) -> None:
    scenario = _default()
    products = [
        p.model_copy(update={"pallet_kg": 300.0}) if p.id == "strawberry" else p
        for p in scenario.products
    ]
    scenario = scenario.model_copy(update={"products": products})
    plan_id = store.create(scenario).plan_id
    _apply(client, plan_id, {"type": "cancel", "lot_id": "lot-jinju-sb-1"})
    session = store.get(plan_id)
    assert session is not None
    order = store.orders(plan_id)[-1]
    rendered = saas_requests(plan_id, order.requests, session.scenario)
    cancel = next(
        r
        for r in rendered
        if r.container_id == "CA-01" and r.kind != "cy_entry" and r.pallets_before
    )
    raw = next(r for r in order.requests if r.request_id == cancel.request_id)
    assert "5,100kg" in raw.text
    assert "화물 총중량 6,120" in cancel.text
    assert "7,040" not in cancel.text


def test_engine6_entered_booking_number_replaces_mock(client: TestClient) -> None:
    plan_id = store.create(_default()).plan_id
    _apply(client, plan_id, REDUCE, "pull_forward")
    order_requests = store.orders(plan_id)[-1].requests
    numbers = {"CA-01": EnteredNumbers(booking_no="HDMU-BK1234")}
    text = next(
        r.text
        for r in saas_requests(plan_id, order_requests, _default(), numbers=numbers)
        if r.kind == "booking_amendment"
    )
    assert "부킹 HDMU-BK1234의" in text
    assert mock_booking_no(plan_id, "CA-01") not in text
    assert "(모의)" not in text
    mock = next(
        r.text
        for r in saas_requests(plan_id, order_requests, _default())
        if r.kind == "booking_amendment"
    )
    assert f"부킹 {mock_booking_no(plan_id, 'CA-01')}(모의)의" in mock


def test_engine6_forwarder_orders_use_saved_booking_number(
    platform_client: TestClient, saas_login, forwarder_client: TestClient
) -> None:
    plan_id = _platform_plan(platform_client, saas_login)["plan_id"]
    _assign(plan_id, FORWARDER_ORG_ID)
    _apply(platform_client, plan_id, REDUCE, "pull_forward")
    sent = forwarder_client.get(f"/api/saas/forwarder/plans/{plan_id}/orders").json()
    amendment = next(r for r in sent[-1]["requests"] if r["kind"] == "booking_amendment")
    container_id = amendment["container_id"]
    saved = forwarder_client.put(
        f"/api/saas/forwarder/plans/{plan_id}/execution-card/containers/{container_id}",
        json={"booking_no": "HDMU-BK1234"},
    )
    assert saved.status_code == 200, saved.text
    sent = forwarder_client.get(f"/api/saas/forwarder/plans/{plan_id}/orders").json()
    text = next(r["text"] for r in sent[-1]["requests"] if r["kind"] == "booking_amendment")
    assert "부킹 HDMU-BK1234의" in text
    assert "(모의)" not in text
    platform = platform_client.get(f"/api/plans/{plan_id}/orders").json()
    assert (
        next(r["text"] for r in platform[-1]["requests"] if r["kind"] == "booking_amendment")
        == text
    )
