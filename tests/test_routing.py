from datetime import datetime, timedelta, timezone

import pytest

from freshswitch_backend.data.loader import get_scenario
from freshswitch_backend.routing import (
    Depot,
    PickupRequest,
    PickupStop,
    PickupTruck,
    build_pickup_request,
    solve_pickup,
)

KST = timezone(timedelta(hours=9))
STRAWBERRY_LOTS = ["lot-jinju-sb-1", "lot-sancheong-sb-1", "lot-miryang-sb-1"]
PORT = Depot(id="KRPUS", lat=35.08, lon=128.83)
DEADLINE = datetime(2026, 11, 17, 18, 0, tzinfo=KST)


def _stop(stop_id: str, lat: float, lon: float, pallets: int, ready: datetime) -> PickupStop:
    return PickupStop(
        id=stop_id, packhouse_id=f"ph-{stop_id}", lat=lat, lon=lon, pallets=pallets, ready_at=ready
    )


def _default_request() -> PickupRequest:
    scenario = get_scenario("default")
    assert scenario is not None
    return build_pickup_request(scenario, STRAWBERRY_LOTS, "V-2611A")


def test_build_request_aggregates_by_packhouse() -> None:
    scenario = get_scenario("default")
    assert scenario is not None
    req = build_pickup_request(scenario, [*STRAWBERRY_LOTS, "lot-miryang-sb-2"], "V-2611A")
    stops = {s.packhouse_id: s for s in req.stops}
    assert set(stops) == {"ph-jinju", "ph-sancheong", "ph-miryang"}
    assert stops["ph-miryang"].pallets == 6
    assert stops["ph-miryang"].lot_ids == ["lot-miryang-sb-1", "lot-miryang-sb-2"]
    assert stops["ph-miryang"].ready_at == datetime(2026, 11, 18, 8, 0, tzinfo=KST)
    assert req.depot.id == "KRPUS"
    assert req.deadline == DEADLINE
    assert [t.id for t in req.trucks] == ["RT-01", "RT-02", "RT-03"]


def test_default_strawberry_pickup_meets_cutoff() -> None:
    req = _default_request()
    plan = solve_pickup(req)
    assert plan.status == "OK"
    assert plan.dropped_stop_ids == []
    visited = [v.stop_id for r in plan.routes for v in r.stops]
    assert sorted(visited) == sorted(s.id for s in req.stops)
    assert len(plan.routes) == 2
    for route in plan.routes:
        assert route.load_pallets <= route.capacity_pallets
        assert route.port_eta <= DEADLINE
        assert route.slack_minutes >= 0
        assert route.earliest_departure <= route.latest_departure
        for visit in route.stops:
            assert visit.ready_at <= visit.eta <= visit.latest_arrival
    assert plan.total_km == pytest.approx(sum(r.km for r in plan.routes), abs=0.2)
    assert plan.total_cost > 0


def test_solver_is_deterministic() -> None:
    req = _default_request()
    a, b = solve_pickup(req), solve_pickup(req)
    assert a.model_dump(exclude={"solve_ms"}) == b.model_dump(exclude={"solve_ms"})


def test_capacity_forces_two_trucks() -> None:
    ready = datetime(2026, 11, 16, 8, 0, tzinfo=KST)
    req = PickupRequest(
        depot=PORT,
        stops=[
            _stop("a", 35.18, 128.11, 6, ready),
            _stop("b", 35.42, 127.87, 6, ready),
            _stop("c", 35.50, 128.75, 6, ready),
        ],
        trucks=[
            PickupTruck(id="T1", capacity_pallets=12),
            PickupTruck(id="T2", capacity_pallets=12),
        ],
        deadline=DEADLINE,
    )
    plan = solve_pickup(req)
    assert plan.dropped_stop_ids == []
    assert len(plan.routes) == 2
    assert all(r.load_pallets <= 12 for r in plan.routes)
    assert sum(r.load_pallets for r in plan.routes) == 18


def test_stop_ready_after_cutoff_is_dropped() -> None:
    req = PickupRequest(
        depot=PORT,
        stops=[
            _stop("ok", 35.18, 128.11, 5, datetime(2026, 11, 17, 8, 0, tzinfo=KST)),
            _stop("late", 35.42, 127.87, 5, datetime(2026, 11, 17, 17, 0, tzinfo=KST)),
        ],
        trucks=[PickupTruck(id="T1", capacity_pallets=16)],
        deadline=DEADLINE,
    )
    plan = solve_pickup(req)
    assert plan.status == "PARTIAL"
    assert plan.dropped_stop_ids == ["late"]
    assert [v.stop_id for r in plan.routes for v in r.stops] == ["ok"]


def test_conflicting_windows_drop_one_stop_in_solver() -> None:
    ready = datetime(2026, 11, 17, 14, 0, tzinfo=KST)
    req = PickupRequest(
        depot=PORT,
        stops=[
            _stop("sangju", 36.41, 128.16, 5, ready),
            _stop("pohang", 36.02, 129.34, 4, ready),
        ],
        trucks=[PickupTruck(id="T1", capacity_pallets=16)],
        deadline=DEADLINE,
    )
    plan = solve_pickup(req)
    assert plan.status == "PARTIAL"
    assert len(plan.dropped_stop_ids) == 1
    assert len(plan.routes) == 1
    assert plan.routes[0].port_eta <= DEADLINE


def test_oversized_stop_is_dropped() -> None:
    ready = datetime(2026, 11, 16, 8, 0, tzinfo=KST)
    req = PickupRequest(
        depot=PORT,
        stops=[_stop("big", 35.18, 128.11, 30, ready)],
        trucks=[PickupTruck(id="T1", capacity_pallets=16)],
        deadline=DEADLINE,
    )
    plan = solve_pickup(req)
    assert plan.routes == []
    assert plan.dropped_stop_ids == ["big"]
