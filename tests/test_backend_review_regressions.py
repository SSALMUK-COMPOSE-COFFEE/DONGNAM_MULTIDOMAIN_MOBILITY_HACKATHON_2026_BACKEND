from datetime import UTC, datetime, timedelta

import httpx
import pytest
from pydantic import SecretStr

from freshswitch_backend.api import access, vessels
from freshswitch_backend.coldchain import coldchain_store
from freshswitch_backend.coldchain import store as coldchain_module
from freshswitch_backend.config import Settings
from freshswitch_backend.data.loader import get_scenario
from freshswitch_backend.explain.prompt import build_payload
from freshswitch_backend.explain.service import ExplanationRejected, validate_output
from freshswitch_backend.integrations.mof_vessel import MofVesselClient
from freshswitch_backend.integrations.ulsan_position import UlsanPositionClient
from freshswitch_backend.main import app
from freshswitch_backend.planning.store import store
from freshswitch_backend.quality import quality_history
from freshswitch_backend.replan import CancelEvent, ReduceEvent, replan
from freshswitch_backend.replan.explanation import build_explain_context


@pytest.fixture(autouse=True)
def isolate_sensor_and_learning():
    coldchain_store.clear()
    quality_history.reset()
    yield
    coldchain_store.clear()
    quality_history.reset()


def create(client):
    response = client.post("/api/plans", json={"scenario_id": "default"})
    assert response.status_code == 201
    return response.json()


@pytest.mark.parametrize("kind", ["busan", "ulsan"])
def test_upstream_http_failure_never_returns_service_key(client, monkeypatch, kind):
    secret = "REVIEW-FAKE-SERVICE-KEY"
    transport = httpx.MockTransport(lambda request: httpx.Response(503, text="unavailable"))
    monkeypatch.setattr(
        vessels, "get_settings", lambda: Settings(offline_mode=False, _env_file=None)
    )
    if kind == "busan":
        dependency = vessels.vessel_client_factory
        factory = lambda: MofVesselClient(secret, transport=transport)
        url = "/api/vessels/busan?date=20261030&direction=O"
    else:
        dependency = vessels.ulsan_client_factory
        factory = lambda: UlsanPositionClient(secret, transport=transport)
        url = "/api/vessels/ulsan"
    monkeypatch.setitem(app.dependency_overrides, dependency, lambda: factory)
    response = client.get(url)
    assert response.status_code == 200
    assert response.json()["source"] == "snapshot"
    assert "503" in response.json()["fallback_reason"]
    assert secret not in response.text and "serviceKey=" not in response.text


def test_reset_disabled_without_token_preserves_all_state(client, monkeypatch):
    session = create(client)
    monkeypatch.setattr(access, "get_settings", lambda: Settings(_env_file=None))
    before = len(quality_history.observations())
    assert client.post("/api/demo/reset").status_code == 403
    assert client.get(f"/api/plans/{session['plan_id']}").status_code == 200
    assert len(quality_history.observations()) == before


def test_reset_requires_correct_bearer_token(client, monkeypatch):
    session = create(client)
    monkeypatch.setattr(
        access,
        "get_settings",
        lambda: Settings(demo_reset_token=SecretStr("admin-test-token"), _env_file=None),
    )
    for headers in (
        {},
        {"Authorization": "Bearer wrong"},
        {"Authorization": "Basic admin-test-token"},
    ):
        assert client.post("/api/demo/reset", headers=headers).status_code == 401
        assert client.get(f"/api/plans/{session['plan_id']}").status_code == 200
    response = client.post("/api/demo/reset", headers={"Authorization": "Bearer admin-test-token"})
    assert response.status_code == 200
    assert client.get(f"/api/plans/{session['plan_id']}").status_code == 404


def test_sensor_readings_alerts_reset_and_simulation_are_plan_scoped(client):
    first, second = create(client)["plan_id"], create(client)["plan_id"]
    assert (
        client.post(
            "/api/coldchain/bindings",
            json={"device_id": "A", "container_id": "CA-01", "plan_id": first},
        ).status_code
        == 201
    )
    for minute in (0, 30):
        assert (
            client.post(
                "/api/coldchain/readings",
                json={
                    "device_id": "A",
                    "ts": (
                        datetime(2026, 11, 17, tzinfo=UTC) + timedelta(minutes=minute)
                    ).isoformat(),
                    "temp_c": 1,
                    "power_on": False,
                },
            ).status_code
            == 200
        )
    assert (
        client.post(
            "/api/coldchain/bindings",
            json={"device_id": "B", "container_id": "CA-01", "plan_id": second},
        ).status_code
        == 201
    )

    def state(plan):
        response = client.get("/api/coldchain/containers/CA-01", params={"plan_id": plan})
        assert response.status_code == 200
        return response.json()

    assert state(first)["reading_count"] == 2
    assert state(second)["reading_count"] == 0
    assert state(second)["device_ids"] == []
    assert client.get("/api/coldchain/containers/CA-01").status_code == 422
    ambiguous = client.post(
        "/api/coldchain/readings",
        json={"device_id": "unknown", "container_id": "CA-01", "temp_c": 1, "power_on": True},
    )
    assert ambiguous.status_code == 422
    assert client.get("/api/coldchain/alerts", params={"plan_id": second}).json()["alerts"] == []
    assert (
        client.post(
            "/api/coldchain/simulate", json={"plan_id": second, "preset": "normal"}
        ).status_code
        == 200
    )
    assert state(first)["reading_count"] == 2
    assert state(first)["outage"]["cumulative_minutes"] == 30
    assert state(second)["outage"]["cumulative_minutes"] == 0
    assert client.delete("/api/coldchain/containers/CA-01").status_code == 422
    assert (
        client.delete("/api/coldchain/containers/CA-01", params={"plan_id": second}).status_code
        == 204
    )
    assert state(first)["reading_count"] == 2


def test_changed_quantity_invalidates_inspection_and_rejection_restores_it(client):
    pid = create(client)["plan_id"]
    before = client.post(
        f"/api/plans/{pid}/settlement/arrival",
        json={"lot_id": "lot-sancheong-sb-1", "inspected_loss_pct": 2},
    ).json()
    assert before["refund_after"] > 0
    event = client.post(
        f"/api/plans/{pid}/events",
        json={"event": {"type": "reduce", "lot_id": "lot-sancheong-sb-1", "new_pallets": 3}},
    ).json()
    applied = client.post(
        f"/api/plans/{pid}/apply",
        json={"replan_id": event["replan_id"], "alternative_id": "sail_as_is"},
    )
    assert applied.status_code == 200
    view = client.get(f"/api/plans/{pid}/settlement").json()
    assert view["total_refund"] == 0 and view["inspections"] == []
    order = client.get(f"/api/plans/{pid}/orders").json()[-1]
    confirmation = order["shipper_confirmations"][0]
    rejected = client.post(
        f"/api/plans/{pid}/orders/{order['order_id']}/confirm",
        json={"actor": "shipper", "shipper_id": confirmation["shipper_id"], "accept": False},
    )
    assert rejected.status_code == 200
    view = client.get(f"/api/plans/{pid}/settlement").json()
    assert view["total_refund"] == before["refund_after"]
    assert len(view["inspections"]) == 1


def test_arrival_hook_uses_saved_snapshot_when_plan_is_cancelled_mid_request(client, monkeypatch):
    pid = create(client)["plan_id"]
    original = store.record_arrival_snapshot

    def interleaved(*args, **kwargs):
        result, snapshot = original(*args, **kwargs)
        current = store.get(pid)
        changed = replan(current.scenario, current.plan, CancelEvent(lot_id="lot-pohang-gr-1"))
        pending = store.add_pending(current, changed)
        store.apply(pid, "sail_as_is", pending.replan_id)
        return result, snapshot

    monkeypatch.setattr(store, "record_arrival_snapshot", interleaved)
    response = client.post(
        f"/api/plans/{pid}/settlement/arrival",
        json={"lot_id": "lot-pohang-gr-1", "inspected_loss_pct": 10},
    )
    assert response.status_code == 200
    assert store.get(pid).version == 2
    observation = next(o for o in quality_history.observations() if o.plan_id == pid)
    assert observation.lot_id == "lot-pohang-gr-1" and observation.pallets == 5


def test_older_learning_hook_cannot_overwrite_newer_reinspection(client):
    pid = create(client)["plan_id"]
    from freshswitch_backend.execution.quality import ArrivalRequest

    older, old_snapshot = store.record_arrival_snapshot(
        pid, ArrivalRequest(lot_id="lot-jinju-sb-1", inspected_loss_pct=5)
    )
    newer, new_snapshot = store.record_arrival_snapshot(
        pid, ArrivalRequest(lot_id="lot-jinju-sb-1", inspected_loss_pct=9)
    )
    quality_history.record_arrival(pid, new_snapshot, newer)
    quality_history.record_arrival(pid, old_snapshot, older)
    observations = [o for o in quality_history.observations() if o.plan_id == pid]
    assert len(observations) == 1 and observations[0].actual_loss_rate == 0.09


@pytest.mark.parametrize(
    "promise",
    [
        "보험사가 손실을 전액 보상하며 승인 절차는 생략합니다.",
        "예약이 확정되어 바로 출항합니다.",
        "수수료는 무료입니다.",
    ],
)
def test_extra_llm_promises_rejected_even_with_all_correct_facts(promise):
    session = store.create(get_scenario("default"))
    result = replan(
        session.scenario, session.plan, ReduceEvent(lot_id="lot-sancheong-sb-1", new_pallets=3)
    )
    context = build_explain_context(session.scenario, session.plan, result)
    facts = build_payload(context)
    payload = {
        "operator_summary": facts["verified_operator_summary"],
        "shipper_messages": [
            {"shipper_id": s["shipper_id"], "message": s["required_facts"]}
            for s in facts["shippers"]
        ],
    }
    assert validate_output(context, payload, "mock").generated_by == "llm"
    payload["shipper_messages"][0]["message"] += " " + promise
    with pytest.raises(ExplanationRejected):
        validate_output(context, payload, "mock")


@pytest.mark.parametrize("day", ["20260230", "20261301", "00000000", "20260229"])
def test_invalid_calendar_date_is_validation_error(client, day):
    assert client.get("/api/vessels/busan", params={"date": day}).status_code == 422


def test_instant_simulation_can_be_queried_and_stopped(client, monkeypatch):
    monkeypatch.setattr(coldchain_module, "MAX_RUNS", 2)
    runs = [
        client.post("/api/coldchain/simulate", json={"preset": "normal"}).json() for _ in range(3)
    ]
    assert client.get(f"/api/coldchain/simulate/{runs[0]['run_id']}").status_code == 404
    for run in runs[1:]:
        response = client.get(f"/api/coldchain/simulate/{run['run_id']}")
        assert response.status_code == 200
        assert response.json() == run
        assert client.delete(f"/api/coldchain/simulate/{run['run_id']}").json()["done"]
