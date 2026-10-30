import threading
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from freshswitch_backend.api.analysis import SurchargeSweep
from freshswitch_backend.coldchain import coldchain_store
from freshswitch_backend.coldchain import store as store_module
from freshswitch_backend.coldchain.models import ReadingIn, SimulationRun
from freshswitch_backend.domain.models import KST

START = datetime(2026, 11, 17, 9, 0, tzinfo=KST)


@pytest.fixture(autouse=True)
def clean_coldchain():
    coldchain_store.clear()
    yield
    coldchain_store.clear()


def _reading(minute: float, power_on: bool, container: str = "CA-01", temp: float = 1.0) -> dict:
    return {
        "device_id": "dev-1",
        "container_id": container,
        "ts": (START + timedelta(minutes=minute)).isoformat(),
        "temp_c": temp,
        "power_on": power_on,
    }


def test_eviction_keeps_outage_history(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(store_module, "MAX_READINGS", 100)
    readings = [_reading(i / 2, False, temp=1.0 + i / 200) for i in range(8 * 60 * 2 + 21)]
    for i in range(0, len(readings), 37):
        response = client.post("/api/coldchain/readings", json=readings[i : i + 37])
        assert response.status_code == 200
    state = client.get("/api/coldchain/containers/CA-01").json()
    assert state["reading_count"] == 100
    assert state["outage"]["cumulative_minutes"] == pytest.approx(490, abs=0.01)
    assert state["outage"]["current_continuous_minutes"] == pytest.approx(490, abs=0.01)
    assert state["outage"]["episodes"] == 1
    assert state["outage"]["insurance_eligible"]
    assert state["excursion"]["degree_minutes"] > 0
    kinds = [a["kind"] for a in state["actions"]]
    assert sorted(kinds) == sorted(set(kinds))
    assert "insurance_evidence" in kinds
    assert [a["kind"] for a in state["alerts"]].count("power_cut") == 1
    assert state["timeline"][0]["ts"] == readings[0]["ts"]


def test_single_batch_eviction_keeps_history(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(store_module, "MAX_READINGS", 50)
    readings = [_reading(m, False) for m in range(0, 500, 2)]
    body = client.post("/api/coldchain/readings", json=readings).json()
    assert body["containers"][0]["cumulative_outage_minutes"] == pytest.approx(498)
    kinds = [a["action"]["kind"] for a in body["new_alerts"] if a["action"]]
    assert "insurance_evidence" in kinds


def test_sweep_count_checked_before_materializing() -> None:
    with pytest.raises(ValidationError):
        SurchargeSweep(start=0, stop=20000, step=0.000001)
    with pytest.raises(ValidationError):
        SurchargeSweep(start=0, stop=20000, step=5e-324)
    assert SurchargeSweep(start=0, stop=2000, step=500).values() == [0, 500, 1000, 1500, 2000]


def test_sweep_rejects_tiny_step_and_reversed_range(client: TestClient) -> None:
    tiny = client.post(
        "/api/analysis/fuel-shock", json={"sweep": {"start": 0, "stop": 20000, "step": 5e-324}}
    )
    assert tiny.status_code == 422
    reversed_range = client.post(
        "/api/analysis/fuel-shock", json={"sweep": {"start": 2000, "stop": 0, "step": 500}}
    )
    assert reversed_range.status_code == 422


def test_batch_over_container_cap_does_not_crash(client: TestClient) -> None:
    readings = [_reading(0, True, container=f"C-{i:03d}") for i in range(201)]
    response = client.post("/api/coldchain/readings", json=readings)
    assert response.status_code == 200
    assert response.json()["accepted"] == 201
    assert len(response.json()["containers"]) == 201
    assert len(coldchain_store.summaries()) == store_module.MAX_CONTAINERS


def test_stopped_simulation_cannot_recreate_reset_container() -> None:
    store = coldchain_store
    cid = "CA-RACE"
    resetters: list[threading.Thread] = []

    class RacyEvent(threading.Event):
        def is_set(self) -> bool:
            value = super().is_set()
            if not value and not resetters:
                resetter = threading.Thread(target=store.reset_container, args=(cid,))
                resetters.append(resetter)
                resetter.start()
                resetter.join(0.3)
            return value

    reading = ReadingIn(device_id="dev-1", container_id=cid, ts=START, temp_c=1.0, power_on=False)
    info = SimulationRun(
        run_id="race",
        preset="normal",
        container_id=cid,
        plan_id=None,
        device_id="dev-1",
        mode="stream",
        total=1,
        sent=0,
        interval_seconds=0.01,
        done=False,
        start=START,
        end=START,
    )
    run = store_module._Run(info=info, readings=[reading], stop=RacyEvent())
    store._runs["race"] = run
    store._stream(run)
    resetters[0].join(5)
    assert cid not in {s.container_id for s in store.summaries()}
    assert store.alerts(container_id=cid).alerts == []


def test_late_reading_does_not_duplicate_episode_actions(client: TestClient) -> None:
    client.post("/api/coldchain/readings", json=[_reading(65, False), _reading(95, False)])
    first = client.get("/api/coldchain/containers/CA-01").json()
    assert len(first["actions"]) == 2
    late = client.post("/api/coldchain/readings", json=[_reading(60, False)]).json()
    assert late["accepted"] == 1
    assert [a for a in late["new_alerts"] if a["kind"] in ("action", "power_cut")] == []
    state = client.get("/api/coldchain/containers/CA-01").json()
    assert len(state["actions"]) == 2
    assert [a["kind"] for a in state["alerts"]].count("power_cut") == 1
    assert state["outage"]["current_continuous_minutes"] == pytest.approx(35)
    client.post("/api/coldchain/readings", json=[_reading(130, True), _reading(200, False)])
    after = client.get("/api/coldchain/containers/CA-01").json()
    assert [a["kind"] for a in after["alerts"]].count("power_cut") == 2
    assert [a["kind"] for a in after["alerts"]].count("power_restored") == 1
