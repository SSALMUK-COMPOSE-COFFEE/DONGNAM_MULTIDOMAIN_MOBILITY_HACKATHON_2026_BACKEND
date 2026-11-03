import pytest
from fastapi import HTTPException, Request
from fastapi.testclient import TestClient
from saas_fixtures import PLATFORM_ORG_ID, login_client
from sqlalchemy import func, select
from test_add_lot import _plan_with_spare
from test_change_requests import PLATFORM, _plan, _request

from freshswitch_backend.api import compute_limits
from freshswitch_backend.api.auth import Principal, RateRule
from freshswitch_backend.config import get_settings
from freshswitch_backend.main import create_app
from freshswitch_backend.persistence import database
from freshswitch_backend.persistence.tables import ChangeRequestRow

EVENT = {"event": {"type": "reduce", "lot_id": "lot-jinju-sb-1", "new_pallets": 2}}


@pytest.fixture
def gate(monkeypatch) -> compute_limits.ComputeGate:
    fresh = compute_limits.ComputeGate(slots=1, wait_seconds=0.05)
    monkeypatch.setattr(compute_limits, "gate", fresh)
    return fresh


@pytest.fixture
def saas_app(demo_tenants):
    return create_app(get_settings().model_copy(update={"saas_mode": True}))


def _busy(response) -> None:
    assert response.status_code == 503, response.text
    assert response.json()["detail"] == compute_limits.COMPUTE_BUSY
    assert response.headers["Retry-After"] == str(compute_limits.RETRY_AFTER_SECONDS)


def _change_requests() -> int:
    with database.begin() as db:
        return db.scalar(select(func.count()).select_from(ChangeRequestRow)) or 0


def _demo_plan(client: TestClient) -> str:
    created = client.post("/api/plans", json={"scenario_id": "default"})
    assert created.status_code == 201, created.text
    return created.json()["plan_id"]


def test_demo_event_needs_compute_slot(client, gate) -> None:
    plan_id = _demo_plan(client)
    with gate.slot():
        _busy(client.post(f"/api/plans/{plan_id}/events", json=EVENT))
    assert client.post(f"/api/plans/{plan_id}/events", json=EVENT).status_code == 200


def test_platform_event_needs_compute_slot(platform_client, saas_login, gate) -> None:
    plan_id = _plan(platform_client, saas_login)
    with gate.slot():
        _busy(platform_client.post(f"/api/plans/{plan_id}/events", json=EVENT))
    assert platform_client.post(f"/api/plans/{plan_id}/events", json=EVENT).status_code == 200


def test_shipper_change_request_needs_compute_slot(platform_client, saas_login, gate) -> None:
    plan_id = _plan(platform_client, saas_login)
    shipper = saas_login("shipper-pohang")
    with gate.slot():
        _busy(_request(shipper, plan_id))
    assert _change_requests() == 0
    assert _request(shipper, plan_id).status_code == 201


def test_platform_change_request_replan_needs_compute_slot(
    platform_client, saas_login, gate
) -> None:
    plan_id = _plan(platform_client, saas_login)
    created = _request(saas_login("shipper-pohang"), plan_id)
    assert created.status_code == 201, created.text
    request_id = created.json()["request_id"]
    with gate.slot():
        _busy(platform_client.post(f"{PLATFORM}/{request_id}/replan"))
    assert platform_client.get(f"{PLATFORM}/{request_id}").json()["replan_id"] is None
    assert platform_client.post(f"{PLATFORM}/{request_id}/replan").status_code == 200


def test_add_lot_needs_compute_slot(platform_client, saas_login, gate) -> None:
    plan_id, extra = _plan_with_spare(platform_client, saas_login)
    path = f"/api/saas/platform/plans/{plan_id}/add-lot"
    with gate.slot():
        _busy(platform_client.post(path, json={"lot_id": extra}))
    assert platform_client.post(path, json={"lot_id": extra}).status_code == 200


def test_replan_rate_limit_per_visitor(saas_app, monkeypatch) -> None:
    monkeypatch.setattr(compute_limits, "REPLAN_RATE", RateRule(per_client=2, window_seconds=60))
    platform = login_client(PLATFORM_ORG_ID, saas_app)
    plan_id = _plan(platform, lambda org_id: login_client(org_id, saas_app))
    path = f"/api/plans/{plan_id}/events"
    assert [platform.post(path, json=EVENT).status_code for _ in range(2)] == [200, 200]
    limited = platform.post(path, json=EVENT)
    assert limited.status_code == 429
    assert int(limited.headers["Retry-After"]) >= 1
    add_lot = f"/api/saas/platform/plans/{plan_id}/add-lot"
    assert platform.post(add_lot, json={"lot_id": "lot-x"}).status_code == 429
    assert platform.post(f"{PLATFORM}/missing/replan").status_code == 429


def test_shipper_change_requests_share_replan_rate(saas_app, monkeypatch) -> None:
    monkeypatch.setattr(compute_limits, "REPLAN_RATE", RateRule(per_client=1, window_seconds=60))
    platform = login_client(PLATFORM_ORG_ID, saas_app)
    plan_id = _plan(platform, lambda org_id: login_client(org_id, saas_app))
    shipper = login_client("shipper-pohang", saas_app)
    assert _request(shipper, plan_id).status_code == 201
    limited = _request(shipper, plan_id)
    assert limited.status_code == 429
    assert _change_requests() == 1


def _fake_request(client_app, host: str = "testclient") -> Request:
    return Request({"type": "http", "app": client_app, "headers": [], "client": (host, 50000)})


def test_replan_org_cap_spans_visitors(saas_app, monkeypatch) -> None:
    monkeypatch.setattr(
        compute_limits, "REPLAN_ORG_RATE", RateRule(per_client=2, window_seconds=60)
    )
    for host in ("10.0.0.1", "10.0.0.2"):
        principal = Principal(f"user-{host}", PLATFORM_ORG_ID, "platform", "admin")
        compute_limits.check_replan_rate(_fake_request(saas_app, host), principal)
    late = Principal("user-late", PLATFORM_ORG_ID, "platform", "admin")
    with pytest.raises(HTTPException) as caught:
        compute_limits.check_replan_rate(_fake_request(saas_app, "10.0.0.3"), late)
    assert caught.value.status_code == 429
    assert caught.value.headers["Retry-After"] == "60"
    elsewhere = Principal("user-late", "platform-other", "platform", "admin")
    compute_limits.check_replan_rate(_fake_request(saas_app, "10.0.0.3"), elsewhere)


def test_replan_rate_is_off_for_anonymous_demo(client, monkeypatch) -> None:
    monkeypatch.setattr(compute_limits, "REPLAN_RATE", RateRule(per_client=1, window_seconds=60))
    plan_id = _demo_plan(client)
    codes = [client.post(f"/api/plans/{plan_id}/events", json=EVENT).status_code for _ in range(3)]
    assert codes == [200, 200, 200]


def test_default_replan_rates_are_generous() -> None:
    assert compute_limits.REPLAN_RATE.per_client >= 20
    assert compute_limits.REPLAN_ORG_RATE.per_client >= compute_limits.PREVIEW_ORG_RATE.per_client
