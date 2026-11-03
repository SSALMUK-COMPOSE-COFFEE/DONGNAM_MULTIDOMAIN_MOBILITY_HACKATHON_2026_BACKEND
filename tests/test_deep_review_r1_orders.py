from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from freshswitch_backend.domain.models import KST, Quantity
from freshswitch_backend.execution import orders as orders_module
from freshswitch_backend.execution.params import ExecutionParams
from freshswitch_backend.planning.store import store


def _create(client: TestClient) -> str:
    return client.post("/api/plans", json={"scenario_id": "default"}).json()["plan_id"]


def _alternatives(client: TestClient, plan_id: str, event: dict) -> dict:
    response = client.post(f"/api/plans/{plan_id}/events", json={"event": event})
    assert response.status_code == 200, response.text
    return {a["id"]: a for a in response.json()["alternatives"]}


def _apply(client: TestClient, plan_id: str, alternative: str) -> None:
    response = client.post(f"/api/plans/{plan_id}/apply", json={"alternative_id": alternative})
    assert response.status_code == 200, response.text


def _orders(client: TestClient, plan_id: str) -> list[dict]:
    return client.get(f"/api/plans/{plan_id}/orders").json()


def _live(client: TestClient, plan_id: str, now: datetime) -> dict:
    order = _orders(client, plan_id)[-1]
    assert order["status"] in ("proposed", "shipper_confirmed"), order["status"]
    deadlines = order["deadlines"]
    shipper = datetime.fromisoformat(deadlines["shipper"])
    forwarder = datetime.fromisoformat(deadlines["forwarder"])
    assert now < shipper < forwarder
    for request in order["requests"]:
        assert datetime.fromisoformat(request["deadline"]) >= forwarder
    for item in order["shipper_confirmations"]:
        assert now < datetime.fromisoformat(item["deadline"]) <= forwarder
    return order


def test_replan3_order_created_after_air_deadline_is_not_born_expired(client: TestClient) -> None:
    plan_id = _create(client)
    now = datetime(2026, 11, 17, 9, 0, tzinfo=KST)
    store.clock = lambda: now
    alternatives = _alternatives(client, plan_id, {"type": "cancel", "lot_id": "lot-miryang-sb-2"})
    _apply(client, plan_id, next(iter(alternatives)))
    order = _live(client, plan_id, now)
    air = [r for r in order["requests"] if r["kind"] == "air_cancel"]
    assert air
    assert "하한" in order["deadlines"]["basis"]
    assert "11/17 00:00" not in air[0]["text"]


def test_replan3_delay_after_cutoff_has_no_fallback_dispatch(client: TestClient) -> None:
    plan_id = _create(client)
    now = datetime(2026, 11, 18, 10, 0, tzinfo=KST)
    store.clock = lambda: now
    event = {
        "type": "delay",
        "voyage_id": "V-2611A",
        "new_etd": "2026-11-19T18:00:00+09:00",
        "source": "해수부",
    }
    alternatives = _alternatives(client, plan_id, event)
    assert "sail_as_is" in alternatives
    _apply(client, plan_id, "sail_as_is")
    order = _live(client, plan_id, now)
    assert "이미 터미널에 반입" in order["deadlines"]["basis"]
    assert any(r["kind"] == "reefer_plug" and "연장" in r["text"] for r in order["requests"])
    session = client.get(f"/api/plans/{plan_id}").json()
    voyage = next(v for v in session["scenario"]["voyages"] if v["id"] == "V-2611A")
    assert voyage["etd"].startswith("2026-11-19T18:00")


def test_replan3_default_deadlines_unchanged(client: TestClient) -> None:
    plan_id = _create(client)
    _alternatives(
        client, plan_id, {"type": "reduce", "lot_id": "lot-sancheong-sb-1", "new_pallets": 3}
    )
    _apply(client, plan_id, "pull_forward")
    order = _orders(client, plan_id)[-1]
    assert "하한" not in order["deadlines"]["basis"]
    dispatch = datetime.fromisoformat(order["deadlines"]["dispatch"])
    forwarder = datetime.fromisoformat(order["deadlines"]["forwarder"])
    assert forwarder <= dispatch


def test_replan3_shipper_only_floor_is_written_in_basis(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    params = ExecutionParams(
        harvest_notice_hours=Quantity(value=24 * 30, unit="시간", source="가정: 테스트")
    )
    monkeypatch.setattr(orders_module, "ExecutionParams", lambda: params)
    now = datetime(2026, 11, 10, 9, 0, tzinfo=KST)
    store.clock = lambda: now
    plan_id = _create(client)
    _alternatives(
        client, plan_id, {"type": "reduce", "lot_id": "lot-sancheong-sb-1", "new_pallets": 3}
    )
    _apply(client, plan_id, "pull_forward")
    order = _orders(client, plan_id)[-1]
    floor = now + timedelta(hours=params.shipper_response_hours.value)
    forwarder_floor = floor + timedelta(hours=params.forwarder_processing_hours.value)
    assert all(datetime.fromisoformat(r["deadline"]) > forwarder_floor for r in order["requests"])
    assert order["shipper_confirmations"]
    basis = order["deadlines"]["basis"]
    assert "하한" in basis
    assert "수확 통보" in basis
    assert datetime.fromisoformat(order["deadlines"]["shipper"]) == floor
    for item in order["shipper_confirmations"]:
        assert datetime.fromisoformat(item["deadline"]) == floor


def _etas(routes: list[dict], container_lots: set[str]) -> set[tuple[str, str]]:
    found = set()
    for voyage_pickup in routes:
        for route in voyage_pickup["pickup"]["routes"]:
            if any(lot in container_lots for stop in route["stops"] for lot in stop["lot_ids"]):
                found.add((route["truck_id"], route["port_eta"]))
    return found


def test_replan12_unchanged_container_with_new_truck_eta_gets_cy_entry(client: TestClient) -> None:
    plan_id = _create(client)
    before = client.get(f"/api/plans/{plan_id}").json()
    alternatives = _alternatives(client, plan_id, {"type": "cancel", "lot_id": "lot-jinju-sb-1"})
    chosen = None
    for alt_id, alternative in alternatives.items():
        if not alternative.get("feasible", True):
            continue
        lots = {
            a["lot_id"]
            for a in alternative["plan"]["assignments"]
            if a["mode"] == "sea" and a["container_id"] == "CA-02"
        }
        same = lots == {
            a["lot_id"]
            for a in before["plan"]["assignments"]
            if a["mode"] == "sea" and a["container_id"] == "CA-02"
        }
        if same and _etas(alternative["routes"], lots) != _etas(before["routes"], lots):
            chosen = alt_id
            break
    assert chosen is not None
    _apply(client, plan_id, chosen)
    order = _orders(client, plan_id)[-1]
    entries = [r for r in order["requests"] if r["kind"] == "cy_entry"]
    assert any(r["container_id"] == "CA-02" for r in entries)
