from datetime import date, datetime

import pytest

from freshswitch_backend.data.loader import get_scenario
from freshswitch_backend.domain.models import Lot, Plan, Scenario
from freshswitch_backend.optimize import AIR, solve_plan
from freshswitch_backend.planning.service import build_plan
from freshswitch_backend.replan import (
    AddLotEvent,
    AirSwitchEvent,
    CancelEvent,
    DelayEvent,
    ReduceEvent,
    ReplanError,
    ReplanResult,
    apply_event,
    locations,
    replan,
)


@pytest.fixture(scope="module")
def scenario() -> Scenario:
    found = get_scenario("default")
    assert found is not None
    return found


@pytest.fixture(scope="module")
def plan(scenario: Scenario) -> Plan:
    return build_plan(scenario).plan


@pytest.fixture(scope="module")
def reduce_result(scenario: Scenario, plan: Plan) -> ReplanResult:
    return replan(scenario, plan, ReduceEvent(lot_id="lot-sancheong-sb-1", new_pallets=3))


def _alt(result: ReplanResult, alt_id: str):
    return next(a for a in result.alternatives if a.id == alt_id)


def _fill(alt, container_id: str) -> int:
    return next(c.pallets_after for c in alt.diff.containers if c.container_id == container_id)


def _shipper(alt, shipper_id: str):
    return next(s for s in alt.diff.shippers if s.shipper_id == shipper_id)


def test_proposal_pull_forward_keeps_ca01_at_17(reduce_result: ReplanResult) -> None:
    alt = _alt(reduce_result, "pull_forward")
    assert alt.feasible
    assert _fill(alt, "CA-01") == 17
    pulled = next(a for a in alt.plan.assignments if a.lot_id == "lot-miryang-sb-2")
    assert pulled.container_id == "CA-01"
    assert pulled.pull_forward_days == 1
    assert [(m.lot_id, m.from_location, m.to_location) for m in alt.diff.moved_lots] == [
        ("lot-miryang-sb-2", AIR, "CA-01")
    ]
    for sid in ("sh-jinju", "sh-sancheong", "sh-miryang"):
        assert _shipper(alt, sid).share_per_pallet_delta_pct == 0.0
    assert reduce_result.recommended_id == "pull_forward"
    assert reduce_result.diff == alt.diff


def test_proposal_next_voyage_rejected_by_sell_by(reduce_result: ReplanResult) -> None:
    alt = _alt(reduce_result, "next_voyage")
    assert not alt.feasible
    assert len(alt.infeasible_reasons) == 3
    assert all("판매기한" in r for r in alt.infeasible_reasons)


def test_proposal_sail_as_is_raises_share_13_percent(reduce_result: ReplanResult) -> None:
    alt = _alt(reduce_result, "sail_as_is")
    assert alt.feasible
    assert _fill(alt, "CA-01") == 15
    for sid in ("sh-jinju", "sh-sancheong", "sh-miryang"):
        assert _shipper(alt, sid).share_per_pallet_delta_pct == pytest.approx(13.3)
    jinju = _shipper(alt, "sh-jinju")
    assert jinju.container_share_after / jinju.container_share_before == pytest.approx(
        17 / 15, 1e-4
    )
    assert alt.total_cost > _alt(reduce_result, "pull_forward").total_cost


def test_replan_is_fast_and_keeps_untouched_container(reduce_result: ReplanResult) -> None:
    assert reduce_result.solve_ms < 10_000
    assert reduce_result.scope == "container"
    assert reduce_result.fixed_lot_ids == ["lot-pohang-gr-1", "lot-sangju-gr-1"]
    for alt in reduce_result.alternatives:
        where = locations(alt.plan)
        assert where["lot-sangju-gr-1"] == where["lot-pohang-gr-1"] == "CA-02"


def test_fixed_decisions_survive_even_if_suboptimal(scenario: Scenario) -> None:
    current = build_plan(scenario, fixed={"lot-sangju-gr-1": "CA-03"}).plan
    assert locations(current)["lot-pohang-gr-1"] == "CA-03"
    result = replan(scenario, current, ReduceEvent(lot_id="lot-sancheong-sb-1", new_pallets=3))
    for alt in result.alternatives:
        where = locations(alt.plan)
        assert where["lot-sangju-gr-1"] == "CA-03"
        assert where["lot-pohang-gr-1"] == "CA-03"
    fresh = solve_plan(apply_event(scenario, result.event), max_pull_forward_days=2)
    assert locations(fresh)["lot-sangju-gr-1"] == "CA-02"


def test_cancel_drops_lot_and_refills(scenario: Scenario, plan: Plan) -> None:
    result = replan(scenario, plan, CancelEvent(lot_id="lot-sancheong-sb-1"))
    pull = _alt(result, "pull_forward")
    assert "lot-sancheong-sb-1" not in locations(pull.plan)
    assert _fill(pull, "CA-01") == 14
    assert _fill(_alt(result, "sail_as_is"), "CA-01") == 12
    gone = next(m for m in pull.diff.moved_lots if m.lot_id == "lot-sancheong-sb-1")
    assert gone.to_location is None
    assert result.recommended_id == "pull_forward"


def test_air_switch_locks_lot_on_air(scenario: Scenario, plan: Plan) -> None:
    result = replan(scenario, plan, AirSwitchEvent(lot_id="lot-sancheong-sb-1"))
    for alt in result.alternatives:
        assert locations(alt.plan)["lot-sancheong-sb-1"] == AIR
    assert _fill(_alt(result, "pull_forward"), "CA-01") == 14
    assert result.recommended_id == "pull_forward"


def test_air_locked_lot_is_not_pulled_back(scenario: Scenario, plan: Plan) -> None:
    result = replan(
        scenario,
        plan,
        ReduceEvent(lot_id="lot-sancheong-sb-1", new_pallets=3),
        air_locked=["lot-miryang-sb-2"],
    )
    pull = _alt(result, "pull_forward")
    assert locations(pull.plan)["lot-miryang-sb-2"] == AIR
    assert _fill(pull, "CA-01") == 15


def test_delay_keeps_cy_cutoff_and_charges_terminal_dwell(scenario: Scenario, plan: Plan) -> None:
    event = DelayEvent(
        voyage_id="V-2611A", new_etd=datetime.fromisoformat("2026-11-19T18:00+09:00")
    )
    result = replan(scenario, plan, event)
    voyage = next(v for v in result.scenario_after.voyages if v.id == "V-2611A")
    assert voyage.cy_cutoff == datetime.fromisoformat("2026-11-17T18:00+09:00")
    assert voyage.original_etd == datetime.fromisoformat("2026-11-18T18:00+09:00")
    pull = _alt(result, "pull_forward")
    late = next(a for a in pull.plan.assignments if a.lot_id == "lot-miryang-sb-2")
    assert (late.container_id, late.pull_forward_days) == ("CA-01", 1)
    as_is = _alt(result, "sail_as_is")
    assert as_is.feasible
    ca01 = next(c for c in as_is.plan.containers if c.container_id == "CA-01")
    assert ca01.terminal_extra_days == 1.0
    assert ca01.terminal_plug_cost == 50_000
    jinju_before = next(c for c in plan.shipper_costs if c.shipper_id == "sh-jinju").plan
    jinju_after = next(c for c in as_is.plan.shipper_costs if c.shipper_id == "sh-jinju").plan
    plug_share = 50_000 * 8 / 17
    assert jinju_after.inland - jinju_before.inland == pytest.approx(8 * 3000 + plug_share, abs=1)
    assert jinju_after.loss_risk > jinju_before.loss_risk


def test_delay_with_new_cutoff_lets_late_lot_board_without_pull(
    scenario: Scenario, plan: Plan
) -> None:
    event = DelayEvent(
        voyage_id="V-2611A",
        new_etd=datetime.fromisoformat("2026-11-19T18:00+09:00"),
        new_cy_cutoff=datetime.fromisoformat("2026-11-18T18:00+09:00"),
    )
    result = replan(scenario, plan, event)
    pull = _alt(result, "pull_forward")
    late = next(a for a in pull.plan.assignments if a.lot_id == "lot-miryang-sb-2")
    assert (late.container_id, late.pull_forward_days) == ("CA-01", 0)
    voyage = next(v for v in result.scenario_after.voyages if v.id == "V-2611A")
    assert voyage.cy_cutoff == datetime.fromisoformat("2026-11-18T18:00+09:00")


def test_earlier_departure_moves_cutoff_with_it(scenario: Scenario) -> None:
    event = DelayEvent(
        voyage_id="V-2611A", new_etd=datetime.fromisoformat("2026-11-18T12:00+09:00")
    )
    voyage = next(v for v in apply_event(scenario, event).voyages if v.id == "V-2611A")
    assert voyage.cy_cutoff == datetime.fromisoformat("2026-11-17T12:00+09:00")


def test_every_alternative_carries_its_own_routes(reduce_result: ReplanResult) -> None:
    for alt in reduce_result.alternatives:
        sea = {a.lot_id for a in alt.plan.assignments if a.mode == "sea"}
        routed = {lid for vp in alt.routes for lid in vp.lot_ids}
        assert routed == sea
        assert alt.plan.collection_basis == "route"
        for vp in alt.routes:
            for route in vp.pickup.routes:
                allocated = sum(
                    c.route
                    for c in alt.plan.lot_collections
                    if c.voyage_id == vp.voyage_id
                    and c.truck_id == route.truck_id
                    and c.trip == route.trip
                )
                assert allocated == round(route.cost)
    pull = _alt(reduce_result, "pull_forward")
    assert "lot-miryang-sb-2" in {lid for vp in pull.routes for lid in vp.lot_ids}


def test_long_delay_breaks_sell_by_for_strawberries(scenario: Scenario, plan: Plan) -> None:
    event = DelayEvent(
        voyage_id="V-2611A",
        new_etd=datetime.fromisoformat("2026-11-21T06:00+09:00"),
        source="해수부",
    )
    result = replan(scenario, plan, event)
    as_is = _alt(result, "sail_as_is")
    assert not as_is.feasible
    assert any("판매기한" in r for r in as_is.infeasible_reasons)
    pull = _alt(result, "pull_forward")
    assert pull.feasible
    where = locations(pull.plan)
    assert where["lot-jinju-sb-1"] == AIR
    assert result.recommended_id == "pull_forward"


def test_add_lot_fills_free_space(scenario: Scenario, plan: Plan) -> None:
    lot = Lot(
        id="lot-jinju-sb-2",
        shipper_id="sh-jinju",
        product_id="strawberry",
        pallets=2,
        ready_date=date(2026, 11, 16),
        destination="HKHKG",
        sell_by_date=date(2026, 11, 23),
    )
    result = replan(scenario, plan, AddLotEvent(lot=lot))
    pull = _alt(result, "pull_forward")
    assert locations(pull.plan)["lot-jinju-sb-2"] == "CA-01"
    assert locations(_alt(result, "sail_as_is").plan)["lot-jinju-sb-2"] == AIR
    assert not _alt(result, "next_voyage").feasible
    assert result.recommended_id == "pull_forward"


@pytest.mark.parametrize(
    "event",
    [
        CancelEvent(lot_id="missing"),
        ReduceEvent(lot_id="lot-sancheong-sb-1", new_pallets=5),
        DelayEvent(voyage_id="missing", new_etd=datetime.fromisoformat("2026-11-19T18:00+09:00")),
    ],
)
def test_invalid_events_raise(scenario: Scenario, plan: Plan, event) -> None:
    with pytest.raises(ReplanError):
        replan(scenario, plan, event)
