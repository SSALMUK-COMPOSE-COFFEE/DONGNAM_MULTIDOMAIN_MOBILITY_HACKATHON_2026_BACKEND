import time
from datetime import date, datetime, timedelta

import pytest

from freshswitch_backend.data.loader import get_scenario
from freshswitch_backend.forecast.history import (
    DepartureRecord,
    all_records,
    operator_of,
    terminal_of,
    to_record,
)
from freshswitch_backend.forecast.predictor import (
    PRIOR_P50_HOURS,
    PRIOR_P90_HOURS,
    DelayPredictor,
    DelayQuery,
    quantile,
)
from freshswitch_backend.forecast.service import (
    ForecastOptions,
    air_switch_proposals,
    delay_event,
    forecast_voyages,
)
from freshswitch_backend.integrations.mof_vessel import KST, VesselMovement
from freshswitch_backend.planning.service import build_plan

WED = datetime(2026, 11, 18, 18, 0, tzinfo=KST)


def record(delay: float, terminal="신항 2부두", name="HMM ALPHA", kind="풀컨테이너선", when=WED):
    return DepartureRecord(
        call_sign=name[:6],
        vessel_name=name,
        vessel_kind=kind,
        terminal=terminal,
        planned_departure_at=when,
        departure_at=when + timedelta(hours=delay),
        delay_hours=delay,
    )


def test_quantile_interpolates():
    assert quantile([0, 10], 0.5) == 5
    assert quantile([1, 2, 3, 4, 5], 0.9) == pytest.approx(4.6)


def test_to_record_filters_and_normalizes():
    movement = VesselMovement(
        port_code="020",
        call_sign="X1",
        vessel_name="CMA CGM TEST",
        gross_tonnage=50000,
        departure_berth="신항 2부두 1선석",
        arrival_at=WED - timedelta(days=1),
        planned_departure_at=WED,
        departure_at=WED + timedelta(hours=5),
    )
    r = to_record(movement)
    assert r is not None and r.delay_hours == 5 and r.terminal == "신항 2부두"
    assert r.operator == "CMA CGM" and r.weekday == 2
    long_stay = movement.model_copy(update={"arrival_at": WED - timedelta(days=30)})
    assert to_record(long_stay) is None
    tug = movement.model_copy(update={"gross_tonnage": 200})
    assert to_record(tug) is None
    assert terminal_of("감만부두 3선석(신선대감만터미널)") == "감만부두"
    assert terminal_of("부산신항 2부두") == "신항 2부두"
    assert operator_of("MV FRESH HARMONY") is None


def test_no_data_uses_labeled_prior():
    forecast = DelayPredictor([]).predict(DelayQuery(vessel_name="MV NEW", terminal="신항 9부두"))
    assert forecast.p50_hours == PRIOR_P50_HOURS
    assert forecast.p90_hours == PRIOR_P90_HOURS
    assert forecast.confidence == "low"
    assert [b.level for b in forecast.basis] == ["prior"]
    assert any(a.source.startswith("가정") for a in forecast.assumptions)


def test_falls_back_from_vessel_to_terminal_to_global():
    records = [record(2.0) for _ in range(40)]
    records += [record(10.0, terminal="감만부두", name="EVER X") for _ in range(40)]
    predictor = DelayPredictor(records)
    unknown_terminal = predictor.predict(DelayQuery(vessel_name="MV NEW", terminal="신항 9부두"))
    assert [b.level for b in unknown_terminal.basis] == ["prior", "global", "container"]
    known = predictor.predict(
        DelayQuery(vessel_name="MV NEW", terminal="부산신항 2부두", planned_departure=WED.date())
    )
    assert [b.level for b in known.basis][-2:] == ["terminal", "terminal_weekday"]
    assert known.p50_hours < unknown_terminal.p50_hours
    vessel = predictor.predict(DelayQuery(vessel_name="EVER X", terminal="신항 2부두"))
    assert vessel.basis[-1].level == "vessel" and vessel.basis[-1].sample_size == 40
    assert vessel.confidence == "high"


def test_sparse_level_is_skipped_and_shrunk():
    records = [record(0.0, terminal="감만부두", name="EVER Y") for _ in range(30)]
    records += [record(20.0, name="LONE SHIP"), record(20.0, name="LONE SHIP")]
    forecast = DelayPredictor(records).predict(DelayQuery(vessel_name="LONE SHIP"))
    assert "vessel" not in [b.level for b in forecast.basis]
    records.append(record(20.0, name="LONE SHIP"))
    forecast = DelayPredictor(records).predict(DelayQuery(vessel_name="LONE SHIP"))
    top = forecast.basis[-1]
    assert top.level == "vessel" and top.weight == pytest.approx(3 / 13, abs=1e-3)
    assert 0 < forecast.p50_hours < 20


def test_weather_and_congestion_assumptions():
    predictor = DelayPredictor([record(4.0) for _ in range(20)])
    query = DelayQuery(terminal="신항 2부두")
    normal = predictor.predict(query)
    busy = predictor.predict(query, congestion="high")
    typhoon = predictor.predict(query, weather="typhoon")
    assert busy.p50_hours == pytest.approx(normal.p50_hours * 1.5, abs=0.1)
    assert typhoon.p50_hours == pytest.approx(normal.p50_hours + 36, abs=0.1)
    assert typhoon.p90_hours == pytest.approx(normal.p90_hours + 72, abs=0.1)
    assert typhoon.confidence == "low"
    assert any("태풍" in a.name for a in typhoon.assumptions)


def test_bundled_history_has_samples():
    records = all_records()
    assert len(records) > 300
    assert any(r.terminal == "신항 2부두" for r in records)


@pytest.fixture(scope="module")
def default_plan():
    scenario = get_scenario("default")
    return scenario, build_plan(scenario).plan


def test_air_switch_trigger_only_when_sell_by_exceeded(default_plan):
    scenario, plan = default_plan
    predictor = DelayPredictor([record(2.0) for _ in range(50)])
    calm = forecast_voyages(predictor, scenario, plan, ForecastOptions())
    assert delay_event(calm) is None
    storm = forecast_voyages(predictor, scenario, plan, ForecastOptions(preset="typhoon"))
    event = delay_event(storm)
    assert event is not None and event.voyage_id == "V-2611A" and event.source == "예측"
    risky = {lot.lot_id for v in storm for lot in v.lots if lot.at_risk}
    assert risky == {"lot-jinju-sb-1", "lot-sancheong-sb-1", "lot-miryang-sb-1"}
    proposals = air_switch_proposals(scenario, storm)
    jinju = next(p for p in proposals if p.lot_id == "lot-jinju-sb-1")
    assert jinju.recommended_airport == "ICN"
    assert {o.airport for o in jinju.options} == {"PUS", "ICN"}
    assert all(o.arrival_date <= date(2026, 11, 23) for o in jinju.options)
    gimhae = sum(p.pallets for p in proposals if p.recommended_airport == "PUS")
    assert gimhae <= 6


def test_min_sell_days_margin_triggers(default_plan):
    scenario, plan = default_plan
    predictor = DelayPredictor([record(2.0) for _ in range(50)])
    forecasts = forecast_voyages(predictor, scenario, plan, ForecastOptions(min_sell_days=3))
    risky = [lot for v in forecasts for lot in v.lots if lot.at_risk]
    assert risky and all("판매 가능 기간" in lot.reason for lot in risky)


def test_delay_forecast_endpoint_typhoon_demo(client):
    plan_id = client.post("/api/plans", json={"scenario_id": "default"}).json()["plan_id"]
    calm = client.post(f"/api/plans/{plan_id}/delay-forecast", json={})
    assert calm.status_code == 200
    assert calm.json()["triggered"] is False and calm.json()["replan"] is None
    started = time.perf_counter()
    response = client.post(f"/api/plans/{plan_id}/delay-forecast", json={"preset": "typhoon"})
    assert time.perf_counter() - started < 10
    assert response.status_code == 200
    body = response.json()
    assert body["triggered"] is True
    assert body["options"]["weather"] == "typhoon" and body["options"]["quantile"] == "p90"
    assert body["history"]["record_count"] > 300
    assert set(body["air_lot_ids"]) == {"lot-jinju-sb-1", "lot-sancheong-sb-1", "lot-miryang-sb-1"}
    replan = body["replan"]
    assert replan["recommended_id"] == "pull_forward"
    assert all(p["replan_location"] == "AIR" for p in body["air_switch"])
    applied = client.post(
        f"/api/plans/{plan_id}/apply",
        json={"alternative_id": "pull_forward", "replan_id": replan["replan_id"]},
    )
    assert applied.status_code == 200
    modes = {a["lot_id"]: a["mode"] for a in applied.json()["plan"]["assignments"]}
    assert modes["lot-jinju-sb-1"] == "air"


def test_delay_forecast_unknown_plan(client):
    assert client.post("/api/plans/nope/delay-forecast", json={}).status_code == 404
