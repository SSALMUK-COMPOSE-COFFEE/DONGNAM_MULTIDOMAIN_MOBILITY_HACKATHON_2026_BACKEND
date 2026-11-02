import pytest
from fastapi.testclient import TestClient

from freshswitch_backend.analysis import fuel_shock, with_surcharge
from freshswitch_backend.data.loader import get_scenario
from freshswitch_backend.optimize.costs import CostModel
from freshswitch_backend.planning.service import build_plan

FOLLOW_UP_LOTS = {"lot-miryang-sb-2", "lot-jinju-sb-2", "lot-sancheong-sb-2"}


@pytest.fixture(scope="module")
def shock_scenario():
    return get_scenario("fuel-shock")


@pytest.fixture(scope="module")
def shock(shock_scenario):
    return fuel_shock(shock_scenario)


def _without_timing(result):
    return result.model_copy(update={"solve_ms": 0})


def test_surcharge_split_reproduces_previous_air_rate():
    scenario = get_scenario("default")
    params = scenario.cost_params
    assert params.air_freight_per_kg_krw.value == 1681
    assert params.air_fuel_surcharge_per_kg_krw.value == 470
    assert CostModel(scenario).air_rate_per_kg == 2151
    assert "유류할증료" in params.air_fuel_surcharge_per_kg_krw.source
    plan = build_plan(scenario).plan
    assert plan.total_savings == 10_318_577


def test_with_surcharge_does_not_mutate(shock_scenario):
    copy = with_surcharge(shock_scenario, 2060)
    assert copy.cost_params.air_fuel_surcharge_per_kg_krw.value == 2060
    assert shock_scenario.cost_params.air_fuel_surcharge_per_kg_krw.value == 470


def test_default_values_and_pallet_prices(shock):
    assert [c.surcharge_per_kg for c in shock.cases] == [470, 2060]
    before, after = shock.cases
    assert before.is_baseline and not after.is_baseline
    assert shock.baseline_surcharge_per_kg == 470
    assert before.air_freight_per_pallet == 892_665
    assert after.air_freight_per_pallet == 1_552_515
    assert shock.collection_basis == "route"


def test_surcharge_rise_moves_follow_up_lots_to_sea(shock):
    before, after = shock.cases
    assert not before.plan_changed and before.switches == []
    air_before = {x.lot_id for x in before.lots if x.mode == "air"}
    assert air_before == FOLLOW_UP_LOTS
    assert after.plan_changed
    assert {s.lot_id for s in after.switches} == FOLLOW_UP_LOTS
    assert all(s.direction == "air_to_sea" and s.case == "CA-03" for s in after.switches)
    fill = {c.container_id: c.pallets for c in after.containers if c.used}
    assert fill == {"CA-01": 17, "CA-02": 18, "CA-03": 9}
    assert after.containers_used == before.containers_used + 1
    assert after.air_pallets == 0 and after.sea_pallets == before.sea_pallets + 9
    assert after.savings_delta == after.total_savings - before.total_savings > 0
    by_shipper = {s.shipper_id: s for s in after.shippers}
    assert by_shipper["sh-jinju"].savings_delta > 0
    assert sum(s.savings_delta for s in after.shippers) == after.savings_delta
    assert "항공→해상 3로트 9파렛트" in after.summary
    assert "CA-03 9/20" in after.summary


def test_case_matches_direct_routed_plan(shock_scenario, shock):
    direct = build_plan(with_surcharge(shock_scenario, 2060)).plan
    after = shock.cases[1]
    assert after.total_savings == direct.total_savings
    assert after.total_fee == direct.total_fee
    assert after.total_shared_cost == direct.total_shared_cost


def test_thresholds_bracket_the_flip(shock_scenario, shock):
    found = {t.direction: t for t in shock.thresholds}
    assert found["down"].surcharge_per_kg is None
    up = found["up"]
    assert up.surcharge_per_kg is not None and 470 < up.surcharge_per_kg < 2060
    below = fuel_shock(shock_scenario, [up.surcharge_per_kg - 2], find_thresholds=False)
    at = fuel_shock(shock_scenario, [up.surcharge_per_kg + 1], find_thresholds=False)
    assert not below.cases[0].plan_changed
    assert at.cases[0].plan_changed
    assert {s.lot_id for s in up.switches} == FOLLOW_UP_LOTS


def test_lower_surcharge_from_shocked_baseline_moves_lots_to_air(shock_scenario):
    result = fuel_shock(shock_scenario, [2060, 470, 0], baseline_surcharge=2060)
    base, back, free = result.cases
    assert base.is_baseline and not base.plan_changed
    for case in (back, free):
        assert {s.lot_id for s in case.switches} == FOLLOW_UP_LOTS
        assert all(s.direction == "sea_to_air" for s in case.switches)
        assert case.savings_delta < 0
    assert "해상→항공" in back.summary
    down = next(t for t in result.thresholds if t.direction == "down")
    assert down.surcharge_per_kg is not None and 470 < down.surcharge_per_kg < 2060


def test_default_scenario_keeps_plan_but_savings_grow():
    result = fuel_shock(get_scenario("default"), find_thresholds=False)
    after = result.cases[1]
    assert not after.plan_changed
    assert after.savings_delta == 23_094_750
    assert result.thresholds == []


def test_deterministic(shock_scenario, shock):
    again = fuel_shock(shock_scenario)
    assert _without_timing(again) == _without_timing(shock)


def test_api_default_request(client: TestClient):
    response = client.post("/api/analysis/fuel-shock", json={})
    assert response.status_code == 200
    body = response.json()
    assert body["scenario_id"] == "fuel-shock"
    assert [c["surcharge_per_kg"] for c in body["cases"]] == [470, 2060]
    assert body["cases"][1]["plan_changed"] is True


def test_api_sweep_and_validation(client: TestClient):
    response = client.post(
        "/api/analysis/fuel-shock",
        json={"sweep": {"start": 0, "stop": 2000, "step": 500}, "find_thresholds": False},
    )
    assert response.status_code == 200
    values = [c["surcharge_per_kg"] for c in response.json()["cases"]]
    assert values == [0, 500, 1000, 1500, 2000]
    changed = [c["plan_changed"] for c in response.json()["cases"]]
    assert changed == [False, False, False, True, True]
    bad = client.post("/api/analysis/fuel-shock", json={"surcharges": [-10]})
    assert bad.status_code == 422
    many = client.post(
        "/api/analysis/fuel-shock", json={"sweep": {"start": 0, "stop": 5000, "step": 10}}
    )
    assert many.status_code == 422
    missing = client.post("/api/analysis/fuel-shock", json={"scenario_id": "nope"})
    assert missing.status_code == 404


def test_api_with_plan_id(client: TestClient):
    created = client.post("/api/plans", json={"scenario_id": "fuel-shock"})
    assert created.status_code in (200, 201)
    plan_id = created.json()["plan_id"]
    response = client.post(
        "/api/analysis/fuel-shock", json={"plan_id": plan_id, "find_thresholds": False}
    )
    assert response.status_code == 200
    assert response.json()["plan_id"] == plan_id
