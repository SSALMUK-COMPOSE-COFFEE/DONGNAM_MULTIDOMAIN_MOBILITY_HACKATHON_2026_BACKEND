import asyncio
import threading
import time
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from freshswitch_backend.api import computation, plans
from freshswitch_backend.config import Settings
from freshswitch_backend.evaluation import forecast_backtest, plan_checks
from freshswitch_backend.forecast.history import DepartureRecord
from freshswitch_backend.persistence import database
from freshswitch_backend.persistence.tables import PendingRow
from freshswitch_backend.planning.budget import PlanningTimeout
from freshswitch_backend.planning.store import store


def pending_count(plan_id):
    with database.begin() as db:
        query = select(func.count()).select_from(PendingRow).where(PendingRow.plan_id == plan_id)
        return db.scalar(query)


def history():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    records = []
    for index in range(10):
        planned = start + timedelta(days=index)
        hours = 72 if index == 7 else 2
        records.append(
            DepartureRecord(
                call_sign=f"C{index}",
                vessel_name=f"SHIP {index}",
                vessel_kind="풀컨테이너선",
                planned_departure_at=planned,
                departure_at=planned + timedelta(hours=hours),
                delay_hours=hours,
            )
        )
    return records


def test_forecast_holdout_excludes_future_labels_and_noncontainer_ships():
    records = history()
    noncontainer = records[0].model_copy(update={"call_sign": "OTHER", "vessel_kind": "유조선"})
    report = forecast_backtest(records + [noncontainer, records[0]])
    assert report["train_count"] == 7
    assert report["test_count"] == 2
    assert report["unavailable_training_labels_excluded"] == 1
    assert report["baseline_quantiles_hours"] == {"p50": 2, "p90": 2}
    assert report["global_quantile_baseline"]["p50_mae_hours"] == 0
    assert report["global_quantile_baseline"]["p90_coverage"] == 1
    assert report["global_quantile_baseline"]["p90_pinball_hours"] == 0


def test_forecast_holdout_labels_cannot_change_training_baseline():
    records = history()
    original = forecast_backtest(records)
    for index in (8, 9):
        records[index] = records[index].model_copy(
            update={
                "delay_hours": 100,
                "departure_at": records[index].planned_departure_at + timedelta(hours=100),
            }
        )
    changed = forecast_backtest(records)
    assert changed["baseline_quantiles_hours"] == original["baseline_quantiles_hours"]
    assert changed["global_quantile_baseline"]["p90_coverage"] == 0
    assert changed["global_quantile_baseline"]["p90_pinball_hours"] == pytest.approx(88.2)


def test_forecast_split_rejects_insufficient_data():
    with pytest.raises(ValueError):
        forecast_backtest(history()[:2])
    with pytest.raises(ValueError):
        forecast_backtest(history(), train_fraction=1)


def test_timeout_returns_before_worker_finishes(monkeypatch):
    settings = Settings(replan_timeout_seconds=0.03, _env_file=None)
    monkeypatch.setattr(computation, "get_settings", lambda: settings)
    release = threading.Event()
    entered = threading.Event()

    def slow(*args, **kwargs):
        entered.set()
        release.wait(timeout=2)
        raise PlanningTimeout()

    async def run():
        try:
            with pytest.raises(HTTPException) as error:
                await computation.compute_replan(slow, deadline=time.perf_counter() + 0.03)
            assert error.value.status_code == 503
            assert error.value.detail["code"] == "replan_timeout"
            assert entered.is_set() and not release.is_set()
        finally:
            release.set()

    asyncio.run(run())


def test_expired_request_never_starts_worker():
    def forbidden(**kwargs):
        pytest.fail("Expired request must not start computation")

    async def run():
        with pytest.raises(HTTPException):
            await computation.compute_replan(forbidden, deadline=time.perf_counter() - 1)

    asyncio.run(run())


def test_timeout_does_not_register_partial_plan(client, monkeypatch):
    session = client.post("/api/plans", json={"scenario_id": "default"}).json()
    plan_id = session["plan_id"]
    pending_before = pending_count(plan_id)

    def expired(*args, **kwargs):
        raise PlanningTimeout()

    monkeypatch.setattr(plans, "replan", expired)
    response = client.post(
        f"/api/plans/{plan_id}/events",
        json={"event": {"type": "reduce", "lot_id": "lot-sancheong-sb-1", "new_pallets": 3}},
    )
    assert response.status_code == 503
    assert pending_count(plan_id) == pending_before
    assert client.get(f"/api/plans/{plan_id}").json()["plan"] == session["plan"]


def test_evaluation_detects_missing_pickup_and_changed_fixed_assignment(client):
    session = client.post("/api/plans", json={"scenario_id": "default"}).json()
    stored = store.get(session["plan_id"])
    sea = next(a for a in stored.plan.assignments if a.mode == "sea")
    errors = plan_checks(stored.scenario, stored.plan, [], {sea.lot_id: "AIR"})
    assert any("fixed assignment changed" in error for error in errors)
    assert any("pickup coverage" in error for error in errors)
