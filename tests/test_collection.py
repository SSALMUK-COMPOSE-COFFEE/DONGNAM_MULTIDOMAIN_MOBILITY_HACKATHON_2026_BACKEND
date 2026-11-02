from datetime import date, datetime

import pytest

from freshswitch_backend.data.loader import get_scenario
from freshswitch_backend.domain.models import Scenario
from freshswitch_backend.optimize import AIR
from freshswitch_backend.planning.service import CUTOFF_REASON, PlanWithRoutes, build_plan
from freshswitch_backend.replan import locations


@pytest.fixture(scope="module")
def scenario() -> Scenario:
    found = get_scenario("default")
    assert found is not None
    return found


@pytest.fixture(scope="module")
def built(scenario: Scenario) -> PlanWithRoutes:
    return build_plan(scenario)


def _tight_cutoff(scenario: Scenario) -> Scenario:
    voyages = [
        v.model_copy(update={"cy_cutoff": datetime.fromisoformat("2026-11-17T09:00+09:00")})
        if v.id == "V-2611A"
        else v
        for v in scenario.voyages
    ]
    lots = [
        lot.model_copy(update={"ready_date": date(2026, 11, 17)})
        if lot.id == "lot-sancheong-sb-1"
        else lot
        for lot in scenario.lots
    ]
    return scenario.model_copy(update={"voyages": voyages, "lots": lots})


def test_route_allocation_sums_to_each_route_cost(built: PlanWithRoutes) -> None:
    plan = built.plan
    assert plan.collection_basis == "route"
    pairs = [(vp.voyage_id, r) for vp in built.routes for r in vp.pickup.routes]
    assert pairs
    for voyage_id, route in pairs:
        allocated = sum(
            c.route
            for c in plan.lot_collections
            if c.voyage_id == voyage_id and c.truck_id == route.truck_id and c.trip == route.trip
        )
        assert allocated == round(route.cost)
    total_route = sum(round(r.cost) for _, r in pairs)
    assert sum(c.route for c in plan.lot_collections) == total_route


def test_settlement_uses_route_cost_and_exposes_estimate(built: PlanWithRoutes) -> None:
    plan = built.plan
    by_shipper: dict[str, int] = {}
    for c in plan.lot_collections:
        by_shipper[c.shipper_id] = by_shipper.get(c.shipper_id, 0) + c.route
    sea_only = {"sh-jinju", "sh-sancheong", "sh-sangju", "sh-pohang"}
    for cost in plan.shipper_costs:
        b = cost.operational
        assert b.collection == b.collection_route == cost.plan.collection_route
        assert b.collection_estimate is not None
        assert b.total == b.freight + b.handling + b.collection + b.storage + b.loss_risk
        if cost.shipper_id in sea_only:
            assert b.collection == by_shipper[cost.shipper_id]
    assert plan.total_collection_route == sum(c.operational.collection for c in plan.shipper_costs)
    assert plan.total_collection_estimate == sum(
        c.operational.collection_estimate for c in plan.shipper_costs
    )
    grape = {"sh-sangju", "sh-pohang"}
    for cost in plan.shipper_costs:
        if cost.shipper_id in grape:
            assert cost.plan.total == cost.operational.total
        else:
            assert cost.plan.collection == 0
    assert plan.total_operational_cost == sum(c.operational.total for c in plan.shipper_costs)
    for s in plan.settlements:
        assert s.savings == s.baseline_cost - s.shared_cost
        assert s.fee + s.shipper_gain == s.savings
    assert plan.total_shared_cost == sum(s.shared_cost for s in plan.settlements)


def test_allocation_weights_pallets_times_distance(built: PlanWithRoutes) -> None:
    rows = {c.lot_id: c for c in built.plan.lot_collections}
    sancheong, jinju = rows["lot-sancheong-sb-1"], rows["lot-jinju-sb-1"]
    assert sancheong.truck_id == jinju.truck_id
    assert sancheong.route / sancheong.estimate == pytest.approx(
        jinju.route / jinju.estimate, rel=1e-3
    )


def test_dropped_stop_is_reassigned_with_reason(scenario: Scenario) -> None:
    built = build_plan(_tight_cutoff(scenario))
    plan = built.plan
    assert locations(plan)["lot-sancheong-sb-1"] == AIR
    entry = next(r for r in plan.collection_reassigned if r.lot_id == "lot-sancheong-sb-1")
    assert entry.reason == CUTOFF_REASON
    assert entry.voyage_id == "V-2611A"
    assert entry.location_after == AIR
    assert all(not vp.dropped_lot_ids for vp in built.routes)
    routed = {lid for vp in built.routes for lid in vp.lot_ids}
    assert "lot-sancheong-sb-1" not in routed


def test_dropped_stop_without_air_is_unassigned_with_reason(scenario: Scenario) -> None:
    plan = build_plan(_tight_cutoff(scenario), allow_air=False).plan
    lost = next(u for u in plan.unassigned if u.lot_id == "lot-sancheong-sb-1")
    assert CUTOFF_REASON in lost.reason
    assert "lot-sancheong-sb-1" not in {a.lot_id for a in plan.assignments}


def test_truck_fixed_cost_is_part_of_allocated_route_cost(
    scenario: Scenario, built: PlanWithRoutes
) -> None:
    params = scenario.cost_params.model_copy(deep=True)
    params.reefer_truck_fixed_per_route_krw.value = 100_000
    with_fixed = build_plan(scenario.model_copy(update={"cost_params": params}))
    routes = [r for vp in with_fixed.routes for r in vp.pickup.routes]
    base = sum(round(r.cost) for vp in built.routes for r in vp.pickup.routes)
    assert sum(round(r.cost) for r in routes) == base + 100_000 * len(routes)
    assert with_fixed.plan.total_collection_route == built.plan.total_collection_route + (
        100_000 * len(routes)
    )


def test_routes_never_mix_so2_and_plain_cargo(scenario: Scenario, built: PlanWithRoutes) -> None:
    so2 = {p.id for p in scenario.products if p.so2_treated}
    product_of = {lot.id: lot.product_id for lot in scenario.lots}
    for vp in built.routes:
        for route in vp.pickup.routes:
            kinds = {product_of[lid] in so2 for v in route.stops for lid in v.lot_ids}
            assert len(kinds) == 1


def test_truck_shortage_falls_back_to_reassignment(scenario: Scenario) -> None:
    trucks = [t for t in scenario.trucks if t.id == "RT-01"]
    built = build_plan(scenario.model_copy(update={"trucks": trucks}))
    assert all(not vp.dropped_lot_ids for vp in built.routes)
    assert built.plan.collection_reassigned
    for entry in built.plan.collection_reassigned:
        assert entry.reason.startswith(CUTOFF_REASON)
