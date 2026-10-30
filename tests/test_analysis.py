import pytest

from freshswitch_backend.analysis import (
    benchmark,
    bisect_factor,
    business,
    fcfs_plan,
    greedy_plan,
    sensitivity,
)
from freshswitch_backend.analysis.benchmark import violations
from freshswitch_backend.analysis.params import SPEC_BY_KEY, anchor_value, scaled
from freshswitch_backend.data.loader import get_scenario
from freshswitch_backend.optimize import solve_plan


@pytest.fixture(scope="module")
def scenario():
    return get_scenario("default")


@pytest.fixture(scope="module")
def sens(scenario):
    return sensitivity(scenario)


@pytest.fixture(scope="module")
def bench(scenario):
    return benchmark(scenario)


def test_scaled_does_not_mutate_original(scenario):
    spec = SPEC_BY_KEY["sea_base_loss_rate"]
    before = anchor_value(scenario, spec)
    copy = scaled(scenario, spec, 2.0)
    assert anchor_value(scenario, spec) == before
    assert anchor_value(copy, spec) == pytest.approx(before * 2)
    grape = copy.cost_params.for_product("grape_so2").sea_base_loss_rate.value
    assert grape == pytest.approx(
        scenario.cost_params.for_product("grape_so2").sea_base_loss_rate.value * 2
    )


def test_bisection_converges_to_known_step():
    factor, iterations = bisect_factor(lambda f: f >= 2.37, 5.0)
    assert factor == pytest.approx(2.37, abs=0.005)
    assert factor >= 2.37
    assert iterations < 30
    down, _ = bisect_factor(lambda f: f <= 0.42, 0.0)
    assert down == pytest.approx(0.42, abs=0.005)
    assert bisect_factor(lambda f: False, 5.0) == (None, 1)


def test_sensitivity_is_deterministic(scenario, sens):
    again = sensitivity(scenario)
    strip = {"solve_ms"}
    assert again.model_dump(exclude=strip) == sens.model_dump(exclude=strip)


def test_sensitivity_matches_base_plan(scenario, sens):
    base = solve_plan(scenario)
    assert sens.base_total_savings == base.total_savings
    assert sens.base_total_fee == base.total_fee
    assert {p.param.key for p in sens.params} == set(SPEC_BY_KEY)


def test_tornado_sorted_and_air_freight_dominates(sens):
    impacts = [bar.savings_impact for bar in sens.tornado]
    assert impacts == sorted(impacts, reverse=True)
    assert sens.tornado[0].key == "air_freight_per_kg_krw"


def test_directions_make_sense(sens):
    by_key = {p.param.key: p for p in sens.params}
    assert by_key["sea_ancillary_per_container_krw"].conservative == "high"
    assert by_key["air_freight_per_kg_krw"].conservative == "low"
    assert by_key["loss_rate_per_wait_day"].conservative == "high"
    fee = by_key["platform_fee_rate"]
    assert fee.savings_impact == 0
    assert fee.high.total_fee > fee.low.total_fee


def test_plan_thresholds_flip_routes(scenario, sens):
    found = {(t.key, t.direction): t for t in sens.thresholds}
    ancillary = found["sea_ancillary_per_container_krw", "up"]
    assert ancillary.threshold is not None and ancillary.threshold > ancillary.base
    assert ancillary.changed_lots
    spec = SPEC_BY_KEY["sea_ancillary_per_container_krw"]
    below = solve_plan(scaled(scenario, spec, ancillary.factor - 0.01))
    at = solve_plan(scaled(scenario, spec, ancillary.factor))
    assert {a.lot_id: a.mode for a in below.assignments} != {
        a.lot_id: a.mode for a in at.assignments
    }
    air = found["air_freight_per_kg_krw", "down"]
    assert air.threshold is not None and air.threshold < air.base


def test_replan_thresholds(sens):
    assert sens.replan is not None
    assert sens.replan.base_outcome == "pull_forward"
    harvest = next(t for t in sens.replan.thresholds if t.key == "early_harvest_loss_rate_per_day")
    assert harvest.threshold is not None and harvest.threshold > harvest.base
    assert harvest.flipped_outcome != "pull_forward"


def test_manual_baselines_not_better_than_optimizer(bench):
    methods = {m.method: m for m in bench.methods}
    assert set(methods) == {"optimizer", "fcfs", "greedy", "solo"}
    best = methods["optimizer"]
    assert best.constraint_violations == 0
    assert best.shippers_with_loss == 0
    for name in ("fcfs", "greedy", "solo"):
        other = methods[name]
        assert other.total_baseline_cost == best.total_baseline_cost
        assert best.total_shipper_cost <= other.total_shipper_cost
        assert best.total_savings >= other.total_savings
        assert other.constraint_violations == 0


def test_fcfs_rule_ignores_cost(scenario):
    plan = fcfs_plan(scenario)
    where = {a.lot_id: a.container_id or a.mode for a in plan.assignments}
    assert where["lot-miryang-sb-2"] == "CA-03"
    assert not violations(scenario, plan)


def test_greedy_is_myopic(scenario):
    plan = greedy_plan(scenario)
    optimal = solve_plan(scenario)
    assert plan.total_shared_cost >= optimal.total_shared_cost


def test_replan_benchmark_recommended_beats_manual(bench):
    assert bench.replan is not None
    rows = {r.role: r for r in bench.replan.rows}
    assert rows["manual"].id == "sail_as_is"
    assert bench.replan.recommended_id == "pull_forward"
    assert bench.replan.improvement_vs_manual > 0
    assert rows["recommended"].container_fill["CA-01"] > rows["manual"].container_fill["CA-01"]


def test_business_per_voyage(scenario):
    result = business(scenario, forwarder_share=0.4, ops_cost_per_voyage=2_000_000)
    plan = solve_plan(scenario)
    assert result.total_fee_revenue == pytest.approx(plan.total_fee, abs=2)
    voyage = result.voyages[0]
    assert voyage.forwarder_share == round(voyage.fee_revenue * 0.4)
    assert voyage.platform_net == voyage.fee_revenue - voyage.forwarder_share - 2_000_000
    for container in voyage.containers:
        assert container.breakeven_savings_pallets < container.pallets
    free = business(scenario, forwarder_share=0.0, ops_cost_per_voyage=0)
    assert free.voyages[0].platform_net == free.voyages[0].fee_revenue
    assert result.season is not None
    assert result.season.revenue > 0


def test_endpoints(client):
    sens = client.post("/api/analysis/sensitivity", json={"scenario_id": "default"})
    assert sens.status_code == 200
    assert sens.json()["tornado"]
    bench = client.post("/api/analysis/benchmark", json={"scenario_id": "default"})
    assert bench.status_code == 200
    assert len(bench.json()["methods"]) == 4
    biz = client.get("/api/analysis/business", params={"scenario_id": "default"})
    assert biz.status_code == 200
    assert biz.json()["voyages"]


def test_endpoints_not_found(client):
    assert client.post("/api/analysis/benchmark", json={"scenario_id": "nope"}).status_code == 404
    assert client.get("/api/analysis/business", params={"scenario_id": "nope"}).status_code == 404
    missing = client.post("/api/analysis/sensitivity", json={"plan_id": "nope"})
    assert missing.status_code == 404


def test_sensitivity_with_plan_session(client):
    created = client.post("/api/plans", json={"scenario_id": "default"})
    assert created.status_code == 201
    plan_id = created.json()["plan_id"]
    response = client.post("/api/analysis/sensitivity", json={"plan_id": plan_id})
    assert response.status_code == 200
    assert response.json()["plan_id"] == plan_id
