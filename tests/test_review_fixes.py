import threading
import time
from datetime import UTC, date, datetime
from itertools import pairwise

import pytest
from fastapi.testclient import TestClient

from freshswitch_backend.data.loader import get_scenario
from freshswitch_backend.domain.models import KST, Lot, Scenario
from freshswitch_backend.integrations import VesselMovement, detect_schedule_changes
from freshswitch_backend.optimize import solve_plan
from freshswitch_backend.planning import store as store_module
from freshswitch_backend.planning.service import build_plan, build_routes, solve_with_routes
from freshswitch_backend.planning.store import SessionStore, store
from freshswitch_backend.replan import AirSwitchEvent, ReplanError, replan
from freshswitch_backend.routing import build_pickup_request, solve_pickup

REDUCE = {"type": "reduce", "lot_id": "lot-sancheong-sb-1", "new_pallets": 3}


def _scenario() -> Scenario:
    scenario = get_scenario("default")
    assert scenario is not None
    return scenario


def _with_lots(scenario: Scenario, **updates: dict) -> Scenario:
    lots = [
        lot.model_copy(update=updates[lot.id]) if lot.id in updates else lot
        for lot in scenario.lots
    ]
    return scenario.model_copy(update={"lots": lots})


def _create(client: TestClient) -> str:
    return client.post("/api/plans", json={"scenario_id": "default"}).json()["plan_id"]


def _event(client: TestClient, plan_id: str, event: dict) -> dict:
    response = client.post(f"/api/plans/{plan_id}/events", json={"event": event})
    assert response.status_code == 200, response.text
    return response.json()


def _apply(client: TestClient, plan_id: str, alternative: str) -> dict:
    response = client.post(f"/api/plans/{plan_id}/apply", json={"alternative_id": alternative})
    assert response.status_code == 200, response.text
    return response.json()


def _orders(client: TestClient, plan_id: str) -> list[dict]:
    return client.get(f"/api/plans/{plan_id}/orders").json()


def test_superseding_order_carries_unconfirmed_changes(client: TestClient) -> None:
    plan_id = _create(client)
    _event(client, plan_id, REDUCE)
    _apply(client, plan_id, "pull_forward")
    first = _orders(client, plan_id)[-1]
    pulled = {c["shipper_id"]: c for c in first["shipper_confirmations"]}
    assert "pull_forward" in pulled["sh-miryang"]["reasons"]

    _event(client, plan_id, {"type": "air_switch", "lot_id": "lot-jinju-sb-1"})
    _apply(client, plan_id, "pull_forward")
    second = _orders(client, plan_id)[-1]
    assert second["order_id"] != first["order_id"]
    confirmations = {c["shipper_id"]: c for c in second["shipper_confirmations"]}
    assert "mode_change" in confirmations["sh-sancheong"]["reasons"]
    requests = {r["kind"]: r for r in second["requests"]}
    assert requests["booking_cancel"]["lot_ids"] == [
        "lot-jinju-sb-1",
        "lot-miryang-sb-1",
        "lot-sancheong-sb-1",
    ]
    assert not any("lot-miryang-sb-2" in r["lot_ids"] for r in second["requests"])
    assert second["rollback_version"] == 1

    for sid in confirmations:
        client.post(
            f"/api/plans/{plan_id}/orders/{second['order_id']}/confirm",
            json={"actor": "shipper", "shipper_id": sid, "accept": False},
        )
        break
    session = client.get(f"/api/plans/{plan_id}").json()
    pallets = {a["lot_id"]: a["pallets"] for a in session["plan"]["assignments"]}
    assert pallets["lot-sancheong-sb-1"] == 5


def test_lots_dropped_in_last_round_leave_sea() -> None:
    scenario = _scenario()
    scenario = scenario.model_copy(update={"trucks": scenario.trucks[2:]})
    built = solve_with_routes(scenario, max_rounds=1)
    sea = {a.lot_id for a in built.plan.assignments if a.mode == "sea"}
    assert built.plan.collection_reassigned
    for voyage_pickup in built.routes:
        assert not set(voyage_pickup.dropped_lot_ids) & sea
    for item in built.plan.collection_reassigned:
        assert item.location_after not in {c.id for c in scenario.containers}


def test_trucks_not_double_booked_across_voyages() -> None:
    scenario = _with_lots(
        _scenario(),
        **{
            "lot-sangju-gr-1": {"ready_date": date(2026, 11, 16)},
            "lot-pohang-gr-1": {"ready_date": date(2026, 11, 16)},
        },
    )
    fixed = {
        "lot-jinju-sb-1": "CA-01",
        "lot-sancheong-sb-1": "CA-01",
        "lot-miryang-sb-1": "CA-01",
        "lot-sangju-gr-1": "CA-03",
        "lot-pohang-gr-1": "CA-03",
    }
    plan = solve_plan(scenario, fixed=fixed, require_savings=False)
    routes = build_routes(scenario, plan)
    assert {vp.voyage_id for vp in routes} == {"V-2611A", "V-2611B"}
    busy: dict[str, list[tuple[datetime, datetime]]] = {}
    for vp in routes:
        for route in vp.pickup.routes:
            busy.setdefault(route.truck_id, []).append((route.earliest_departure, route.port_eta))
    for intervals in busy.values():
        intervals.sort()
        for (_, end), (start, _) in pairwise(intervals):
            assert start >= end


def test_air_switch_rejected_when_air_misses_sell_by(client: TestClient) -> None:
    scenario = _with_lots(_scenario(), **{"lot-jinju-sb-1": {"sell_by_date": date(2026, 11, 17)}})
    built = build_plan(scenario)
    with pytest.raises(ReplanError):
        replan(scenario, built.plan, AirSwitchEvent(lot_id="lot-jinju-sb-1"))
    plan_id = _create(client)
    ok = client.post(
        f"/api/plans/{plan_id}/events",
        json={"event": {"type": "air_switch", "lot_id": "lot-jinju-sb-1"}},
    )
    assert ok.status_code == 200


def test_delay_accepts_naive_and_utc_times(client: TestClient) -> None:
    plan_id = _create(client)
    naive = _event(
        client,
        plan_id,
        {"type": "delay", "voyage_id": "V-2611A", "new_etd": "2026-11-19T06:00:00"},
    )
    utc = _event(
        client,
        plan_id,
        {"type": "delay", "voyage_id": "V-2611A", "new_etd": "2026-11-18T21:00:00Z"},
    )
    kst = _event(
        client,
        plan_id,
        {"type": "delay", "voyage_id": "V-2611A", "new_etd": "2026-11-19T06:00:00+09:00"},
    )
    totals = [{a["id"]: a["total_cost"] for a in r["alternatives"]} for r in (naive, utc, kst)]
    assert totals[0] == totals[1] == totals[2]


def test_utc_voyage_times_use_korean_dates() -> None:
    raw = _scenario().model_dump(mode="json")
    for voyage in raw["voyages"]:
        if voyage["id"] == "V-2611A":
            voyage["etd"] = "2026-11-18T09:00:00Z"
            voyage["cy_cutoff"] = "2026-11-17T09:00:00Z"
    scenario = Scenario.model_validate(raw)
    voyage = next(v for v in scenario.voyages if v.id == "V-2611A")
    assert voyage.etd == datetime(2026, 11, 18, 18, 0, tzinfo=KST)
    assert voyage.etd.utcoffset() == KST.utcoffset(None)
    request = build_pickup_request(scenario, ["lot-jinju-sb-1"], "V-2611A")
    assert request.stops[0].ready_at == datetime(2026, 11, 16, 8, 0, tzinfo=KST)


def test_apply_idempotency_is_atomic() -> None:
    local = SessionStore()
    local.clock = lambda: datetime(2026, 11, 10, tzinfo=UTC)
    session = local.create(_scenario())
    result = replan(
        session.scenario,
        session.plan,
        AirSwitchEvent(lot_id="lot-jinju-sb-1"),
        air_locked=session.air_locked_lot_ids,
    )
    pending = local.add_pending(session, result)
    key = ("apply-1", "fp")
    first = local.apply(session.plan_id, "pull_forward", pending.replan_id, idempotency=key)
    second = local.apply(session.plan_id, "pull_forward", pending.replan_id, idempotency=key)
    assert first.version == second.version == 2
    assert len(local.orders(session.plan_id)) == 1


def test_plan_read_expires_overdue_order(client: TestClient) -> None:
    plan_id = _create(client)
    _event(client, plan_id, REDUCE)
    _apply(client, plan_id, "pull_forward")
    store.clock = lambda: datetime(2026, 11, 17, tzinfo=UTC)
    session = client.get(f"/api/plans/{plan_id}").json()
    assert session["status"] == "committed"
    assert session["history"][-1]["kind"] == "rollback"


def test_request_answer_cannot_flip(client: TestClient) -> None:
    plan_id = _create(client)
    _event(client, plan_id, REDUCE)
    _apply(client, plan_id, "pull_forward")
    order = _orders(client, plan_id)[-1]
    base = f"/api/plans/{plan_id}/orders/{order['order_id']}"
    for c in order["shipper_confirmations"]:
        client.post(
            f"{base}/confirm",
            json={"actor": "shipper", "shipper_id": c["shipper_id"], "accept": True},
        )
    client.post(f"{base}/confirm", json={"actor": "forwarder", "accept": True})
    request_id = order["requests"][0]["request_id"]
    assert len(order["requests"]) > 1
    first = client.post(f"{base}/requests/{request_id}", json={"accepted": True})
    assert first.status_code == 200
    again = client.post(f"{base}/requests/{request_id}", json={"accepted": True})
    assert again.status_code == 200
    flipped = client.post(f"{base}/requests/{request_id}", json={"accepted": False})
    assert flipped.status_code == 409


def test_inspection_of_removed_lot_no_longer_refunds(client: TestClient) -> None:
    plan_id = _create(client)
    arrival = client.post(
        f"/api/plans/{plan_id}/settlement/arrival",
        json={"lot_id": "lot-pohang-gr-1", "inspected_loss_pct": 10},
    )
    assert arrival.status_code == 200
    assert arrival.json()["refund_after"] > 0
    _event(client, plan_id, {"type": "cancel", "lot_id": "lot-pohang-gr-1"})
    _apply(client, plan_id, "sail_as_is")
    view = client.get(f"/api/plans/{plan_id}/settlement").json()
    assert view["total_refund"] == 0
    assert view["inspections"] == []


def test_concurrent_arrivals_are_both_kept(client: TestClient, monkeypatch) -> None:
    plan_id = _create(client)
    original = store_module.arrival_settlement

    def slow(*args, **kwargs):
        time.sleep(0.2)
        return original(*args, **kwargs)

    monkeypatch.setattr(store_module, "arrival_settlement", slow)
    lots = ["lot-jinju-sb-1", "lot-sancheong-sb-1"]
    threads = [
        threading.Thread(
            target=client.post,
            args=(f"/api/plans/{plan_id}/settlement/arrival",),
            kwargs={"json": {"lot_id": lot, "inspected_loss_pct": 10}},
        )
        for lot in lots
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    view = client.get(f"/api/plans/{plan_id}/settlement").json()
    assert sorted(i["lot_id"] for i in view["inspections"]) == lots


def test_packhouse_group_larger_than_truck_is_split() -> None:
    scenario = _scenario()
    extra = Lot(
        id="lot-jinju-sb-9",
        shipper_id="sh-jinju",
        product_id="strawberry",
        pallets=8,
        ready_date=date(2026, 11, 16),
        destination="HKHKG",
        sell_by_date=date(2026, 11, 23),
    )
    scenario = scenario.model_copy(update={"lots": [*scenario.lots, extra]})
    request = build_pickup_request(scenario, ["lot-jinju-sb-1", "lot-jinju-sb-9"], "V-2611A")
    assert [s.pallets for s in request.stops] == [16]
    narrow = build_pickup_request(
        scenario.model_copy(
            update={
                "trucks": [t.model_copy(update={"capacity_pallets": 10}) for t in scenario.trucks]
            }
        ),
        ["lot-jinju-sb-1", "lot-jinju-sb-9"],
        "V-2611A",
    )
    assert len(narrow.stops) == 2
    assert not solve_pickup(narrow, time_limit_s=1).dropped_stop_ids


def test_schedule_match_is_bounded_and_one_to_one() -> None:
    movements = [
        VesselMovement(
            port_code="020",
            call_sign="X1",
            vessel_name="MV FRESH HARMONY",
            departure_at=datetime(2026, 11, 18, 12, 0, tzinfo=KST),
        ),
        VesselMovement(
            port_code="020",
            call_sign="X1",
            vessel_name="MV COLD BRIDGE",
            departure_at=datetime(2026, 10, 30, 12, 0, tzinfo=KST),
        ),
    ]
    voyages = [
        ("V-A", "MV FRESH HARMONY", datetime(2026, 11, 18, 18, 0, tzinfo=KST)),
        ("V-A2", "MV FRESH HARMONY", datetime(2026, 11, 19, 18, 0, tzinfo=KST)),
        ("V-B", "MV COLD BRIDGE", datetime(2026, 11, 21, 18, 0, tzinfo=KST)),
    ]
    changes = detect_schedule_changes(voyages, movements)
    assert [c.voyage_id for c in changes] == ["V-A"]
