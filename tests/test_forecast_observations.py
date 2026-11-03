from datetime import date, datetime, timedelta

from sqlalchemy import select

from freshswitch_backend.data.loader import get_scenario
from freshswitch_backend.domain.models import Voyage
from freshswitch_backend.forecast.observations import (
    own_records,
    planned_voyages,
    record_schedule_check,
)
from freshswitch_backend.forecast.predictor import DelayPredictor, default_predictor
from freshswitch_backend.forecast.service import (
    ForecastOptions,
    delay_event,
    forecast_voyages,
    remaining_risks,
)
from freshswitch_backend.integrations import SnapshotStore, VesselMovement
from freshswitch_backend.integrations.mof_vessel import KST
from freshswitch_backend.persistence import database
from freshswitch_backend.persistence.tables import DelayObservationRow
from freshswitch_backend.planning.service import build_plan


def _voyage(etd: datetime, name: str = "MV FRESH HARMONY", call_sign: str | None = None) -> Voyage:
    return Voyage(
        id=f"V-{etd:%m%d}",
        vessel_name=name,
        terminal="부산신항 2부두",
        origin="KRPUS",
        destination="HKHKG",
        etd=etd,
        cy_cutoff=etd - timedelta(days=1),
        transit_days=3,
        call_sign=call_sign,
    )


def _departure(name: str, at: datetime, call_sign: str = "FH01") -> VesselMovement:
    return VesselMovement(
        port_code="020",
        call_sign=call_sign,
        vessel_name=name,
        vessel_kind="풀컨테이너선",
        departure_berth="신항 2부두 1선석",
        departure_at=at,
    )


def _past_voyages(weeks: int = 3) -> tuple[list[Voyage], list[VesselMovement]]:
    base = datetime(2026, 10, 7, 18, 0, tzinfo=KST)
    voyages = [_voyage(base + timedelta(weeks=i)) for i in range(weeks)]
    movements = [_departure(v.vessel_name, v.etd + timedelta(hours=10)) for v in voyages]
    return voyages, movements


def test_planned_voyages_carry_call_sign():
    voyage = _voyage(datetime(2026, 11, 18, 18, 0, tzinfo=KST), call_sign="5LYK5")
    assert planned_voyages([voyage])[0].call_sign == "5LYK5"


def test_real_snapshot_departure_is_recorded_by_call_sign():
    snapshot = SnapshotStore().load(date(2026, 10, 29), "O")
    assert snapshot is not None
    real = next(m for m in snapshot.movements if m.call_sign == "5LYK5")
    voyage = _voyage(datetime(2026, 10, 28, 18, 0, tzinfo=KST), call_sign="5LYK5")
    assert record_schedule_check("demo", [voyage], snapshot.movements, plan_id="p1") == 1
    with database.begin() as db:
        row = db.scalars(select(DelayObservationRow)).one()
    assert row.matched_by == "call_sign" and row.call_sign == "5LYK5"
    assert row.scope == "demo" and row.plan_id == "p1" and row.terminal == "신항 2부두"
    assert row.departure_at == real.departure_at
    assert row.delay_hours == round((real.departure_at - voyage.etd).total_seconds() / 3600, 2)


def test_planned_only_movement_is_not_an_observation():
    voyage = _voyage(datetime(2026, 10, 28, 18, 0, tzinfo=KST))
    movement = VesselMovement(
        port_code="020",
        call_sign="FH01",
        vessel_name=voyage.vessel_name,
        planned_departure_at=voyage.etd + timedelta(hours=5),
    )
    assert record_schedule_check("demo", [voyage], [movement]) == 0
    assert own_records("demo") == []


def test_observations_accumulate_once_per_voyage_and_stay_in_scope(demo_tenants):
    voyages, movements = _past_voyages()
    assert record_schedule_check("demo", voyages, movements) == 3
    assert record_schedule_check("demo", voyages, movements) == 3
    assert len(own_records("demo")) == 3
    assert all(r.delay_hours == 10 for r in own_records("demo"))
    assert own_records("freshswitch") == []
    record_schedule_check("freshswitch", voyages[:1], movements[:1])
    assert len(own_records("freshswitch")) == 1
    assert len(own_records("demo")) == 3


def test_delay_forecast_adds_own_observations(client):
    plan_id = client.post("/api/plans", json={"scenario_id": "default"}).json()["plan_id"]
    before = client.post(f"/api/plans/{plan_id}/delay-forecast?llm=false", json={}).json()
    assert before["history"]["own_observation_count"] == 0
    assert "자체 관측" not in before["summary"]
    voyages, movements = _past_voyages()
    record_schedule_check("demo", voyages, movements, plan_id=plan_id)
    after = client.post(f"/api/plans/{plan_id}/delay-forecast?llm=false", json={}).json()
    history = after["history"]
    assert history["own_observation_count"] == 3
    assert history["record_count"] == before["history"]["record_count"] + 3
    assert history["own_observation_source"].startswith("자체 관측")
    assert "자체 관측 3건 반영" in after["summary"]
    top = after["voyages"][0]["forecast"]["basis"][-1]
    assert top["level"] == "vessel" and top["sample_size"] == 3


def test_remaining_risk_voyages_are_listed():
    scenario = get_scenario("default")
    plan = build_plan(scenario).plan
    storm = forecast_voyages(
        DelayPredictor(default_predictor().records),
        scenario,
        plan,
        ForecastOptions(preset="typhoon"),
    )
    first = storm[0]
    second = first.model_copy(
        update={
            "voyage_id": "V-OTHER",
            "lots": [lot.model_copy(update={"pallets": 1}) for lot in first.lots],
        }
    )
    forecasts = [first, second]
    event = delay_event(forecasts)
    assert event is not None and event.voyage_id == first.voyage_id
    remaining = remaining_risks(forecasts, event)
    assert [r.voyage_id for r in remaining] == ["V-OTHER"]
    assert remaining[0].at_risk_lot_ids == [lot.lot_id for lot in first.lots if lot.at_risk]
    assert remaining_risks(storm, delay_event(storm)) == []
