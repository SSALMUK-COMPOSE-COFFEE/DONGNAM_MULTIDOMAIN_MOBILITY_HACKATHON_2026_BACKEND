import pytest
from fastapi.testclient import TestClient
from saas_fixtures import REAL_PLATFORM_EMAIL, REAL_PLATFORM_PASSWORD, create_real_platform

from freshswitch_backend.config import get_settings
from freshswitch_backend.main import create_app
from freshswitch_backend.quality import quality_history

CSRF = {"X-Requested-With": "freshswitch"}


@pytest.fixture
def saas_client() -> TestClient:
    settings = get_settings().model_copy(update={"saas_mode": True})
    return TestClient(create_app(settings), base_url="https://testserver")


def _demo_login(client: TestClient, org_type: str) -> None:
    response = client.post("/api/auth/demo-login", json={"org_type": org_type})
    assert response.status_code == 200


def _first_observation(_: TestClient) -> str:
    return quality_history.observations()[0].id


def test_saas_anonymous_quality_reset_is_rejected(saas_client: TestClient) -> None:
    before = len(quality_history.observations())
    assert saas_client.post("/api/quality/reset", params={"seed": False}).status_code == 401
    target = _first_observation(saas_client)
    assert saas_client.delete(f"/api/quality/observations/{target}").status_code == 401
    assert len(quality_history.observations()) == before


@pytest.mark.parametrize("org_type", ["shipper", "forwarder"])
def test_saas_non_platform_quality_reset_is_forbidden(
    saas_client: TestClient, demo_tenants, org_type: str
) -> None:
    _demo_login(saas_client, org_type)
    before = len(quality_history.observations())
    reset = saas_client.post("/api/quality/reset", params={"seed": False}, headers=CSRF)
    assert reset.status_code == 403
    target = _first_observation(saas_client)
    deleted = saas_client.delete(f"/api/quality/observations/{target}", headers=CSRF)
    assert deleted.status_code == 403
    assert len(quality_history.observations()) == before


def test_saas_sandbox_platform_cannot_reset_quality(saas_client: TestClient, demo_tenants) -> None:
    _demo_login(saas_client, "platform")
    before = len(quality_history.observations())
    target = _first_observation(saas_client)
    deleted = saas_client.delete(f"/api/quality/observations/{target}", headers=CSRF)
    assert deleted.status_code == 403
    reset = saas_client.post("/api/quality/reset", params={"seed": False}, headers=CSRF)
    assert reset.status_code == 403
    assert len(quality_history.observations()) == before


def test_saas_real_platform_can_reset_quality(saas_client: TestClient) -> None:
    create_real_platform()
    login = saas_client.post(
        "/api/auth/login",
        json={"email": REAL_PLATFORM_EMAIL, "password": REAL_PLATFORM_PASSWORD},
    )
    assert login.status_code == 200, login.text
    target = _first_observation(saas_client)
    deleted = saas_client.delete(f"/api/quality/observations/{target}", headers=CSRF)
    assert deleted.status_code == 204
    reset = saas_client.post("/api/quality/reset", params={"seed": False}, headers=CSRF)
    assert reset.status_code == 200
    assert quality_history.observations() == []


def test_saas_admin_token_can_reset_quality(saas_client: TestClient, reset_admin_headers) -> None:
    wrong = saas_client.post("/api/quality/reset", headers={"Authorization": "Bearer wrong-token"})
    assert wrong.status_code == 401
    reset = saas_client.post(
        "/api/quality/reset", params={"seed": False}, headers=reset_admin_headers
    )
    assert reset.status_code == 200
    assert quality_history.observations() == []


def test_demo_mode_keeps_anonymous_quality_reset(client: TestClient, demo_tenants) -> None:
    assert client.post("/api/quality/reset", params={"seed": False}).status_code == 200
    assert quality_history.observations() == []


def test_demo_mode_logged_in_shipper_cannot_reset_quality(demo_tenants) -> None:
    client = TestClient(create_app(), base_url="https://testserver")
    _demo_login(client, "shipper")
    reset = client.post("/api/quality/reset", params={"seed": False}, headers=CSRF)
    assert reset.status_code == 403
