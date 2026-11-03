from datetime import date

import pytest
from fastapi.testclient import TestClient

from freshswitch_backend.api import vessels
from freshswitch_backend.config import Settings
from freshswitch_backend.integrations import VesselMovement
from freshswitch_backend.main import app

REDUCE = {"type": "reduce", "lot_id": "lot-sancheong-sb-1", "new_pallets": 3}


class FakeClient:
    def __init__(self, movements=None, error: Exception | None = None) -> None:
        self.movements = movements or []
        self.error = error
        self.calls: list[tuple] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info) -> None:
        return None

    async def fetch_movements(self, start: date, end: date, direction=None, **_):
        self.calls.append((start, end, direction))
        if self.error:
            raise self.error
        return self.movements


@pytest.fixture
def fake_vessels():
    holder: dict[str, FakeClient] = {}

    def use(client: FakeClient) -> FakeClient:
        holder["client"] = client
        app.dependency_overrides[vessels.vessel_client_factory] = lambda: lambda: client
        return client

    yield use
    app.dependency_overrides.pop(vessels.vessel_client_factory, None)


def _create(client: TestClient) -> dict:
    response = client.post("/api/plans", json={"scenario_id": "default"})
    assert response.status_code == 201
    return response.json()


def _containers(session: dict) -> dict[str, int]:
    return {c["container_id"]: c["pallets"] for c in session["plan"]["containers"]}


def test_create_event_apply_history(client: TestClient) -> None:
    session = _create(client)
    plan_id = session["plan_id"]
    assert session["version"] == 1
    assert _containers(session)["CA-01"] == 17
    assert session["routes"][0]["voyage_id"] == "V-2611A"
    assert client.get(f"/api/plans/{plan_id}").json()["plan_id"] == plan_id

    replan = client.post(f"/api/plans/{plan_id}/events", json={"event": REDUCE})
    assert replan.status_code == 200
    body = replan.json()
    assert body["recommended_id"] == "pull_forward"
    assert [a["id"] for a in body["alternatives"]] == ["pull_forward", "next_voyage", "sail_as_is"]
    explanation = body["explanation"]
    assert explanation["generated_by"] == "template"
    assert "당겨 채우기" in explanation["operator_summary"]
    assert "-" not in explanation["operator_summary"].split("추천안")[1].split(".")[0]
    assert {m["shipper_id"] for m in explanation["shipper_messages"]} >= {"sh-sancheong"}
    for alt in body["alternatives"]:
        assert alt["plan"]["collection_basis"] == "route"
        assert alt["routes"]
    assert body["solve_ms"] < 10_000
    assert "scenario_after" not in body

    rejected = client.post(f"/api/plans/{plan_id}/apply", json={"alternative_id": "next_voyage"})
    assert rejected.status_code == 409

    applied = client.post(f"/api/plans/{plan_id}/apply", json={"alternative_id": "pull_forward"})
    assert applied.status_code == 200
    updated = applied.json()
    assert updated["version"] == 2
    assert _containers(updated)["CA-01"] == 17
    sancheong = next(
        lot for lot in updated["scenario"]["lots"] if lot["id"] == "lot-sancheong-sb-1"
    )
    assert sancheong["pallets"] == 3
    pickup = updated["routes"][0]["pickup"]
    assert pickup["status"] == "OK"
    assert not pickup["dropped_stop_ids"]

    stale = client.post(
        f"/api/plans/{plan_id}/apply",
        json={"alternative_id": "pull_forward", "replan_id": body["replan_id"]},
    )
    assert stale.status_code == 409

    history = client.get(f"/api/plans/{plan_id}/history").json()
    assert len(history) == 1
    assert history[0]["event"] == REDUCE
    assert history[0]["alternative_id"] == "pull_forward"
    assert history[0]["cost_delta"] < 0


def test_air_switch_then_followup_keeps_short_window(client: TestClient) -> None:
    plan_id = _create(client)["plan_id"]
    event = {"type": "air_switch", "lot_id": "lot-sancheong-sb-1"}
    client.post(f"/api/plans/{plan_id}/events", json={"event": event})
    applied = client.post(f"/api/plans/{plan_id}/apply", json={"alternative_id": "pull_forward"})
    assert applied.json()["air_locked_lot_ids"] == []
    lots = client.get(f"/api/plans/{plan_id}").json()["scenario"]["lots"]
    assert next(x for x in lots if x["id"] == "lot-sancheong-sb-1")["sell_window"] == "short"
    follow = client.post(
        f"/api/plans/{plan_id}/events",
        json={"event": {"type": "reduce", "lot_id": "lot-jinju-sb-1", "new_pallets": 6}},
    ).json()
    for alt in follow["alternatives"]:
        modes = {a["lot_id"]: a["mode"] for a in alt["plan"]["assignments"]}
        assert modes["lot-sancheong-sb-1"] == "air"


def test_not_found_and_validation(client: TestClient) -> None:
    missing = "00000000-0000-0000-0000-000000000000"
    assert client.get(f"/api/plans/{missing}").status_code == 404
    assert client.get(f"/api/plans/{missing}/history").status_code == 404
    assert client.post(f"/api/plans/{missing}/events", json={"event": REDUCE}).status_code == 404
    assert (
        client.post(f"/api/plans/{missing}/apply", json={"alternative_id": "pull_forward"})
    ).status_code == 404
    assert client.post("/api/plans", json={"scenario_id": "nope"}).status_code == 404

    plan_id = _create(client)["plan_id"]
    bad_lot = client.post(
        f"/api/plans/{plan_id}/events", json={"event": {"type": "cancel", "lot_id": "nope"}}
    )
    assert bad_lot.status_code == 422
    assert "nope" in bad_lot.json()["detail"]
    bad_type = client.post(f"/api/plans/{plan_id}/events", json={"event": {"type": "x"}})
    assert bad_type.status_code == 422
    nothing = client.post(f"/api/plans/{plan_id}/apply", json={"alternative_id": "pull_forward"})
    assert nothing.status_code == 409
    unknown = client.post(
        f"/api/plans/{plan_id}/apply",
        json={"alternative_id": "pull_forward", "replan_id": "nope"},
    )
    assert unknown.status_code == 404


def test_compare_air_vs_sea(client: TestClient) -> None:
    response = client.post(
        "/api/compare/air-vs-sea", json={"scenario_id": "default", "lot_id": "lot-jinju-sb-1"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["plan_mode"] == "sea"
    assert body["sea"]["in_plan"] and body["sea"]["container_id"] == "CA-01"
    assert body["cheaper"] == "sea"
    assert body["sea_savings"] == body["air"]["cost"]["total"] - body["sea"]["cost"]["total"]

    late = client.post(
        "/api/compare/air-vs-sea", json={"scenario_id": "default", "lot_id": "lot-miryang-sb-2"}
    ).json()
    assert late["plan_mode"] == "air"
    assert late["sea"]["pull_forward_days"] == 1
    assert late["sea"]["container_pallets_after"] == 19

    assert (
        client.post("/api/compare/air-vs-sea", json={"scenario_id": "default", "lot_id": "x"})
    ).status_code == 404


def test_explanation_llm_flag_needs_setting(client: TestClient, monkeypatch) -> None:
    from freshswitch_backend.replan import explanation as replan_explanation

    calls: list = []

    async def fake_explain(ctx, **kwargs):
        calls.append(ctx)
        from freshswitch_backend.explain import render_template

        return render_template(ctx).model_copy(update={"generated_by": "llm", "model": "fake"})

    monkeypatch.setattr(replan_explanation, "explain", fake_explain)
    plan_id = _create(client)["plan_id"]
    off = Settings(explain_use_llm=False, llm_api_key="k", llm_model_name="m", _env_file=None)
    monkeypatch.setattr(replan_explanation, "get_settings", lambda: off)
    body = client.post(
        f"/api/plans/{plan_id}/events", params={"llm": "true"}, json={"event": REDUCE}
    ).json()
    assert body["explanation"]["generated_by"] == "template"
    assert calls == []

    on = off.model_copy(update={"explain_use_llm": True})
    monkeypatch.setattr(replan_explanation, "get_settings", lambda: on)
    body = client.post(
        f"/api/plans/{plan_id}/events", params={"llm": "false"}, json={"event": REDUCE}
    ).json()
    assert body["explanation"]["generated_by"] == "template"
    body = client.post(
        f"/api/plans/{plan_id}/events", params={"llm": "true"}, json={"event": REDUCE}
    ).json()
    assert body["explanation"]["generated_by"] == "llm"
    assert len(calls) == 1


def test_schedule_check_builds_delay_events(client: TestClient, fake_vessels) -> None:
    plan_id = _create(client)["plan_id"]
    late = VesselMovement(
        port_code="020",
        call_sign="ABC",
        vessel_name="FRESH HARMONY",
        departure_at="2026-11-19T06:00:00+09:00",
    )
    fake = fake_vessels(FakeClient([late]))
    response = client.post(f"/api/plans/{plan_id}/schedule-check")
    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "해수부"
    assert fake.calls == [(date(2026, 11, 17), date(2026, 11, 21), "O")]
    assert body["changes"][0]["delta_hours"] == 12.0
    event = body["events"][0]
    assert event == {
        "type": "delay",
        "voyage_id": "V-2611A",
        "new_etd": "2026-11-19T06:00:00+09:00",
        "new_cy_cutoff": None,
        "source": "해수부",
    }
    replan = client.post(f"/api/plans/{plan_id}/events", json={"event": event})
    assert replan.status_code == 200

    provided = client.post(
        f"/api/plans/{plan_id}/schedule-check",
        json={"movements": [late.model_dump(mode="json")], "threshold_hours": 24},
    ).json()
    assert provided["source"] == "provided"
    assert provided["events"] == []


def test_schedule_check_demo_yields_delay_event(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(vessels, "get_settings", lambda: Settings(offline_mode=True))
    plan_id = _create(client)["plan_id"]
    response = client.post(f"/api/plans/{plan_id}/schedule-check", params={"demo": "true"})
    assert response.status_code == 200
    events = response.json()["events"]
    assert [e["voyage_id"] for e in events] == ["V-2611A"]
    assert events[0]["new_cy_cutoff"] is None
    replan = client.post(f"/api/plans/{plan_id}/events", json={"event": events[0]})
    assert replan.status_code == 200
