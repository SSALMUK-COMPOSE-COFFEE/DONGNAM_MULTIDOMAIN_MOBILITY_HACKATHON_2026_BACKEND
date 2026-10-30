from datetime import datetime, time, timedelta, timezone

from freshswitch_backend.data.loader import get_scenario
from freshswitch_backend.planning.service import build_plan
from freshswitch_backend.routing import (
    Depot,
    GateWindow,
    PickupRequest,
    PickupStop,
    PickupTruck,
    build_pickup_request,
    solve_pickup,
)

KST = timezone(timedelta(hours=9))
PORT = Depot(id="KRPUS", lat=35.08, lon=128.83)
MORNING = GateWindow(start=time(7, 0), end=time(10, 0), label="오전 반입 집중")
EVENING = GateWindow(start=time(15, 0), end=time(18, 0), label="CY 마감 직전 반입 집중")


def _request(ready: datetime, deadline: datetime, windows: list[GateWindow]) -> PickupRequest:
    return PickupRequest(
        depot=PORT,
        stops=[
            PickupStop(
                id="stop-miryang",
                packhouse_id="ph-miryang",
                lat=35.50,
                lon=128.75,
                pallets=4,
                ready_at=ready,
            )
        ],
        trucks=[PickupTruck(id="RT-01", capacity_pallets=16)],
        deadline=deadline,
        gate_windows=windows,
    )


def _in_window(at: datetime, window: GateWindow) -> bool:
    return window.start <= at.astimezone(KST).time() < window.end


def test_without_windows_routes_are_unchanged() -> None:
    ready = datetime(2026, 11, 16, 8, 0, tzinfo=KST)
    deadline = datetime(2026, 11, 17, 18, 0, tzinfo=KST)
    plan = solve_pickup(_request(ready, deadline, []))
    assert plan.gate_mode == "none"
    route = plan.routes[0]
    assert route.gate_status == "off_peak"
    assert route.gate_delay_minutes == 0
    assert route.gate_slot_start is None and route.gate_slot_end is None
    assert _in_window(route.port_eta, MORNING)


def test_morning_peak_is_avoided_by_departing_later() -> None:
    ready = datetime(2026, 11, 16, 8, 0, tzinfo=KST)
    deadline = datetime(2026, 11, 17, 18, 0, tzinfo=KST)
    plain = solve_pickup(_request(ready, deadline, [])).routes[0]
    plan = solve_pickup(_request(ready, deadline, [MORNING, EVENING]))
    assert plan.gate_mode == "avoid"
    route = plan.routes[0]
    assert route.gate_status == "avoided"
    assert route.gate_window == MORNING.text()
    assert route.port_eta == datetime(2026, 11, 16, 10, 0, tzinfo=KST)
    assert route.gate_delay_minutes == (route.port_eta - plain.port_eta).total_seconds() / 60
    assert route.earliest_departure - plain.earliest_departure == route.port_eta - plain.port_eta
    assert route.km == plain.km and route.cost == plain.cost
    assert route.gate_slot_start == datetime(2026, 11, 16, 10, 0, tzinfo=KST)
    assert route.gate_slot_end == datetime(2026, 11, 16, 15, 0, tzinfo=KST)
    assert route.earliest_departure <= route.latest_departure
    assert route.port_eta <= deadline
    assert any("게이트 혼잡" in line for line in plan.assumptions)


def test_peak_allowed_when_avoiding_would_miss_cutoff() -> None:
    ready = datetime(2026, 11, 17, 14, 0, tzinfo=KST)
    deadline = datetime(2026, 11, 17, 17, 30, tzinfo=KST)
    plan = solve_pickup(_request(ready, deadline, [MORNING, EVENING]))
    assert plan.dropped_stop_ids == []
    assert plan.gate_mode == "fallback"
    route = plan.routes[0]
    assert route.gate_status == "peak"
    assert route.gate_window == EVENING.text()
    assert route.gate_delay_minutes == 0
    assert route.port_eta <= deadline
    assert route.gate_slot_start == datetime(2026, 11, 17, 15, 0, tzinfo=KST)
    assert route.gate_slot_end == datetime(2026, 11, 17, 18, 0, tzinfo=KST)


def test_default_scenario_port_has_assumed_gate_peaks() -> None:
    scenario = get_scenario("default")
    assert scenario is not None
    port = next(p for p in scenario.ports if p.id == "KRPUS")
    assert len(port.gate_peak_windows) == 2
    assert all(w.source.startswith("가정") for w in port.gate_peak_windows)
    req = build_pickup_request(scenario, ["lot-miryang-sb-1"], "V-2611A")
    assert [w.text() for w in req.gate_windows] == [MORNING.text(), EVENING.text()]


def test_default_plan_routes_avoid_gate_peaks() -> None:
    scenario = get_scenario("default")
    assert scenario is not None
    built = build_plan(scenario)
    assert built.plan.total_collection_route == 2_354_984
    windows = [MORNING, EVENING]
    statuses = []
    for voyage in built.routes:
        assert voyage.pickup.gate_mode == "avoid"
        for route in voyage.pickup.routes:
            statuses.append(route.gate_status)
            assert not any(_in_window(route.port_eta, w) for w in windows)
            assert route.port_eta <= voyage.pickup.deadline
            assert route.gate_slot_start is not None and route.gate_slot_end is not None
            assert route.gate_slot_start <= route.port_eta < route.gate_slot_end
    assert statuses.count("avoided") == 2
    assert statuses.count("off_peak") == 2
