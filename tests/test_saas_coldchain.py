import hashlib

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from freshswitch_backend.coldchain import DeviceDenied, coldchain_store
from freshswitch_backend.coldchain.models import BindingRequest, ReadingIn
from freshswitch_backend.config import get_settings
from freshswitch_backend.data.loader import get_scenario
from freshswitch_backend.lots import ANY_ORG, lot_store
from freshswitch_backend.main import create_app
from freshswitch_backend.persistence import database
from freshswitch_backend.persistence.seed import DEMO_EMAIL_DOMAIN
from freshswitch_backend.persistence.tables import (
    DEMO_ORG,
    BindingRow,
    ContainerRow,
    DeviceRow,
    PlanForwarderRow,
)
from freshswitch_backend.planning.store import store

CSRF = {"X-Requested-With": "freshswitch"}
PASSWORD = "test-demo-password"
PLATFORM = "freshswitch"
SHIPPER = "shipper-jinju"
FORWARDER = "forwarder-dongnam"
OUTSIDER_SHIPPER = "shipper-sancheong"
OUTSIDER_FORWARDER = "forwarder-sinhang"
LOT_ID = "lot-jinju-sb-1"


def _app():
    settings = get_settings().model_copy(update={"saas_mode": True})
    return create_app(settings)


@pytest.fixture
def app(demo_tenants):
    return _app()


def _login(app, org_id: str) -> TestClient:
    client = TestClient(app, base_url="https://testserver")
    response = client.post(
        "/api/auth/login",
        json={"email": f"{org_id}@{DEMO_EMAIL_DOMAIN}", "password": PASSWORD},
    )
    assert response.status_code == 200, response.text
    assert response.json()["active_org"]["org_id"] == org_id
    return client


def _anonymous(app) -> TestClient:
    return TestClient(app, base_url="https://testserver")


@pytest.fixture
def plan_id(demo_tenants) -> str:
    scenario = get_scenario("default")
    assert scenario is not None
    lot = next(item for item in scenario.lots if item.id == LOT_ID)
    lot_store.create(lambda _: (lot, scenario.id, None), org_id=SHIPPER)
    session = store.create(
        scenario, registered_lot_ids=[LOT_ID], org_id=PLATFORM, lot_org_id=ANY_ORG
    )
    with database.begin() as db:
        db.add(PlanForwarderRow(plan_id=session.plan_id, forwarder_org_id=FORWARDER))
    return session.plan_id


@pytest.fixture
def simulated(app, plan_id: str) -> str:
    platform = _login(app, PLATFORM)
    run = platform.post(
        "/api/coldchain/simulate",
        json={"preset": "power_cut_restore", "plan_id": plan_id, "container_id": "CA-01"},
        headers=CSRF,
    )
    assert run.status_code == 200, run.text
    assert run.json()["done"] is True
    return plan_id


def test_platform_simulation_is_stored_under_plan_owner(app, simulated: str) -> None:
    with database.begin() as db:
        rows = db.execute(select(ContainerRow.record_key, ContainerRow.org_id)).all()
    assert rows == [(f"{simulated}:CA-01", PLATFORM)]
    anonymous = _anonymous(app)
    assert anonymous.get("/api/coldchain/containers").json() == []
    assert anonymous.get("/api/coldchain/alerts").json()["alerts"] == []
    hidden = anonymous.get("/api/coldchain/containers/CA-01", params={"plan_id": simulated})
    assert hidden.status_code == 404


@pytest.mark.parametrize("org_id", [PLATFORM, FORWARDER, SHIPPER])
def test_plan_participants_can_read_plan_coldchain(app, simulated: str, org_id: str) -> None:
    client = _login(app, org_id)
    params = {"plan_id": simulated}
    containers = client.get("/api/coldchain/containers", params=params)
    assert containers.status_code == 200
    assert [c["container_id"] for c in containers.json()] == ["CA-01"]
    state = client.get("/api/coldchain/containers/CA-01", params=params)
    assert state.status_code == 200
    assert state.json()["reading_count"] > 0
    alerts = client.get("/api/coldchain/alerts", params=params)
    assert alerts.status_code == 200
    assert alerts.json()["alerts"]
    stream = client.get("/api/coldchain/alerts/stream", params={**params, "since": 0, "timeout": 0})
    assert stream.status_code == 200
    assert stream.headers["content-type"].startswith("text/event-stream")
    assert stream.text.count("event: alert") == len(alerts.json()["alerts"])
    last = alerts.json()["alerts"][-1]["seq"]
    resumed = client.get(
        "/api/coldchain/alerts/stream", params={**params, "since": last, "timeout": 0}
    )
    assert resumed.text.count("event: alert") == 0


@pytest.mark.parametrize("org_id", [OUTSIDER_SHIPPER, OUTSIDER_FORWARDER])
def test_other_orgs_cannot_read_plan_coldchain(app, simulated: str, org_id: str) -> None:
    client = _login(app, org_id)
    params = {"plan_id": simulated}
    assert client.get("/api/coldchain/containers", params=params).status_code == 404
    assert client.get("/api/coldchain/containers/CA-01", params=params).status_code == 404
    assert client.get("/api/coldchain/alerts", params=params).status_code == 404
    stream = client.get("/api/coldchain/alerts/stream", params={**params, "since": 0, "timeout": 0})
    assert stream.status_code == 404
    preview = client.get("/api/coldchain/containers/CA-01/settlement-preview", params=params)
    assert preview.status_code == 404
    assert client.get("/api/coldchain/containers").json() == []
    assert client.get("/api/coldchain/alerts").json()["alerts"] == []


def test_signed_up_org_cannot_read_other_plan(app, simulated: str) -> None:
    client = _anonymous(app)
    signup = client.post(
        "/api/auth/signup",
        json={
            "email": "new-shipper@example.com",
            "password": "password-123",
            "name": "새 화주",
            "org_name": "새 화주 회사",
            "org_type": "shipper",
        },
    )
    assert signup.status_code == 201
    stream = client.get("/api/coldchain/alerts/stream", params={"plan_id": simulated, "timeout": 0})
    assert stream.status_code == 404


def test_shipper_sees_other_shippers_anonymized(app, simulated: str) -> None:
    client = _login(app, SHIPPER)
    state = client.get("/api/coldchain/containers/CA-01", params={"plan_id": simulated}).json()
    names = {s["shipper_id"]: s["shipper_name"] for s in state["loss"]["shippers"]}
    assert names
    scenario = get_scenario("default")
    real = {s.id: s.name for s in scenario.shippers}
    for shipper_id, name in names.items():
        if shipper_id == "sh-jinju":
            assert name == real["sh-jinju"]
        else:
            assert shipper_id.startswith("화주 ")
            assert name == shipper_id
    others = sorted(name for shipper_id, name in names.items() if shipper_id != "sh-jinju")
    assert others == [f"화주 {chr(ord('A') + i)}" for i in range(len(others))]
    for lot in state["loss"]["lots"]:
        assert lot["shipper_id"] == "sh-jinju" or lot["shipper_id"].startswith("화주 ")
    platform = _login(app, PLATFORM)
    full = platform.get("/api/coldchain/containers/CA-01", params={"plan_id": simulated}).json()
    assert {s["shipper_id"] for s in full["loss"]["shippers"]} <= set(real)


def test_forwarder_does_not_see_money(app, simulated: str) -> None:
    client = _login(app, FORWARDER)
    params = {"plan_id": simulated}
    state = client.get("/api/coldchain/containers/CA-01", params=params).json()
    assert state["loss"]["lots"] == []
    assert state["loss"]["shippers"] == []
    assert state["loss"]["delta_krw"] is None
    summaries = client.get("/api/coldchain/containers", params=params).json()
    assert all(item["delta_krw"] is None for item in summaries)
    preview = client.get("/api/coldchain/containers/CA-01/settlement-preview", params=params)
    assert preview.status_code == 403


def test_shipper_settlement_preview_only_has_own_lots(app, simulated: str) -> None:
    client = _login(app, SHIPPER)
    preview = client.get(
        "/api/coldchain/containers/CA-01/settlement-preview", params={"plan_id": simulated}
    )
    assert preview.status_code == 200
    body = preview.json()
    assert all(lot["shipper_id"] == "sh-jinju" for lot in body["lots"])
    assert body["total_refund_change"] == sum(lot["refund_change"] for lot in body["lots"])


def test_device_registration_returns_token_once_and_stores_hash(app) -> None:
    platform = _login(app, PLATFORM)
    created = platform.post(
        "/api/saas/devices", json={"device_id": "dev-p1", "name": "1번 센서"}, headers=CSRF
    )
    assert created.status_code == 201
    token = created.json()["device_token"]
    assert len(token) >= 32
    with database.begin() as db:
        row = db.get(DeviceRow, "dev-p1")
        assert row.org_id == PLATFORM
        assert row.token_hash == hashlib.sha256(token.encode()).hexdigest()
    listed = platform.get("/api/saas/devices").json()
    assert [d["device_id"] for d in listed] == ["dev-p1"]
    assert "device_token" not in listed[0]
    again = platform.post("/api/saas/devices", json={"device_id": "dev-p1"}, headers=CSRF)
    assert again.status_code == 409
    other = _login(app, FORWARDER)
    assert other.get("/api/saas/devices").json() == []
    assert other.delete("/api/saas/devices/dev-p1", headers=CSRF).status_code == 404
    taken = other.post("/api/saas/devices", json={"device_id": "dev-p1"}, headers=CSRF)
    assert taken.status_code == 409


def test_devices_require_login_and_csrf(app) -> None:
    anonymous = _anonymous(app)
    assert anonymous.get("/api/saas/devices").status_code == 401
    assert anonymous.post("/api/saas/devices", json={"device_id": "x"}).status_code == 401
    platform = _login(app, PLATFORM)
    assert platform.post("/api/saas/devices", json={"device_id": "x"}).status_code == 403


def test_registration_replaces_demo_binding(app) -> None:
    anonymous = _anonymous(app)
    bound = anonymous.post(
        "/api/coldchain/bindings", json={"device_id": "arduino-01", "container_id": "CA-01"}
    )
    assert bound.status_code == 201
    platform = _login(app, PLATFORM)
    created = platform.post("/api/saas/devices", json={"device_id": "arduino-01"}, headers=CSRF)
    assert created.status_code == 201
    assert created.json()["binding"] is None
    with database.begin() as db:
        assert db.get(BindingRow, "arduino-01") is None
    assert anonymous.get("/api/coldchain/bindings").json() == []
    rebind = anonymous.post(
        "/api/coldchain/bindings", json={"device_id": "arduino-01", "container_id": "CA-01"}
    )
    assert rebind.status_code == 403
    sent = anonymous.post(
        "/api/coldchain/readings",
        json={"device_id": "arduino-01", "container_id": "CA-01", "temp_c": 1.0, "power_on": True},
    )
    assert sent.status_code == 401
    simulated = anonymous.post("/api/coldchain/simulate", json={"device_id": "arduino-01"})
    assert simulated.status_code == 403


def test_registered_device_readings_need_token(app, plan_id: str) -> None:
    platform = _login(app, PLATFORM)
    token = platform.post("/api/saas/devices", json={"device_id": "dev-p1"}, headers=CSRF).json()[
        "device_token"
    ]
    bound = platform.post(
        "/api/coldchain/bindings",
        json={"device_id": "dev-p1", "container_id": "CA-01", "plan_id": plan_id},
        headers=CSRF,
    )
    assert bound.status_code == 201, bound.text
    device = _anonymous(app)
    reading = {"device_id": "dev-p1", "temp_c": 1.5, "power_on": True}
    missing = device.post("/api/coldchain/readings", json=reading)
    assert missing.status_code == 401
    wrong = device.post(
        "/api/coldchain/readings", json=reading, headers={"X-Device-Token": token + "x"}
    )
    assert wrong.status_code == 401
    ok = device.post("/api/coldchain/readings", json=reading, headers={"X-Device-Token": token})
    assert ok.status_code == 200, ok.text
    assert ok.json()["accepted"] == 1
    assert ok.json()["containers"][0]["plan_id"] == plan_id
    with database.begin() as db:
        assert db.get(DeviceRow, "dev-p1").last_seen_at is not None
        keys = db.scalars(select(ContainerRow.org_id)).all()
    assert keys == [PLATFORM]
    assert device.get("/api/coldchain/containers").json() == []
    listed = platform.get("/api/saas/devices").json()
    assert listed[0]["binding"]["container_id"] == "CA-01"


def test_device_token_from_other_device_is_rejected(app) -> None:
    platform = _login(app, PLATFORM)
    first = platform.post("/api/saas/devices", json={"device_id": "dev-a"}, headers=CSRF)
    platform.post("/api/saas/devices", json={"device_id": "dev-b"}, headers=CSRF)
    token = first.json()["device_token"]
    device = _anonymous(app)
    wrong = device.post(
        "/api/coldchain/readings",
        json={"device_id": "dev-b", "container_id": "CA-01", "temp_c": 1.0, "power_on": True},
        headers={"X-Device-Token": token},
    )
    assert wrong.status_code == 401
    mixed = device.post(
        "/api/coldchain/readings",
        json=[
            {"device_id": "dev-a", "container_id": "CA-01", "temp_c": 1.0, "power_on": True},
            {"device_id": "loose-01", "container_id": "CA-01", "temp_c": 1.0, "power_on": True},
        ],
        headers={"X-Device-Token": token},
    )
    assert mixed.status_code == 422


def test_logged_in_org_cannot_use_unregistered_device(app, plan_id: str) -> None:
    platform = _login(app, PLATFORM)
    bound = platform.post(
        "/api/coldchain/bindings",
        json={"device_id": "loose-01", "container_id": "CA-01", "plan_id": plan_id},
        headers=CSRF,
    )
    assert bound.status_code == 422
    sent = platform.post(
        "/api/coldchain/readings",
        json={"device_id": "loose-01", "container_id": "CA-01", "temp_c": 1.0, "power_on": True},
        headers=CSRF,
    )
    assert sent.status_code == 422


def test_delete_device_removes_binding(app, plan_id: str) -> None:
    platform = _login(app, PLATFORM)
    platform.post("/api/saas/devices", json={"device_id": "dev-p1"}, headers=CSRF)
    platform.post(
        "/api/coldchain/bindings",
        json={"device_id": "dev-p1", "container_id": "CA-01", "plan_id": plan_id},
        headers=CSRF,
    )
    assert platform.delete("/api/saas/devices/dev-p1", headers=CSRF).status_code == 204
    with database.begin() as db:
        assert db.get(DeviceRow, "dev-p1") is None
        assert db.get(BindingRow, "dev-p1") is None
    assert platform.get("/api/coldchain/bindings").json() == []


def test_store_rejects_foreign_registered_device(app) -> None:
    platform = _login(app, PLATFORM)
    platform.post("/api/saas/devices", json={"device_id": "dev-p1"}, headers=CSRF)
    with pytest.raises(DeviceDenied):
        coldchain_store.bind(BindingRequest(device_id="dev-p1", container_id="CA-01"))
    result = coldchain_store.ingest(
        [ReadingIn(device_id="dev-p1", container_id="CA-01", temp_c=1.0, power_on=True)]
    )
    assert result.accepted == 0
    assert result.rejected[0].device_id == "dev-p1"


def test_anonymous_demo_flow_unchanged(client: TestClient) -> None:
    bound = client.post(
        "/api/coldchain/bindings", json={"device_id": "arduino-01", "container_id": "CA-01"}
    )
    assert bound.status_code == 201
    sent = client.post(
        "/api/coldchain/readings", json={"device_id": "arduino-01", "temp_c": 1.0, "power_on": True}
    )
    assert sent.status_code == 200
    assert sent.json()["accepted"] == 1
    with database.begin() as db:
        assert db.scalars(select(ContainerRow.org_id)).all() == [DEMO_ORG]
    stream = client.get("/api/coldchain/alerts/stream", params={"timeout": 0})
    assert stream.status_code == 200
