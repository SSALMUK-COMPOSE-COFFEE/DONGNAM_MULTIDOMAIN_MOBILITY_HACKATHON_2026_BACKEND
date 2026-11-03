from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient

from freshswitch_backend.main import app
from freshswitch_backend.persistence import database
from freshswitch_backend.persistence.seed import create_platform, demo_orgs

SAAS_CSRF = {"X-Requested-With": "freshswitch"}
SAAS_DEMO_PASSWORD = "test-demo-password"
PLATFORM_ORG_ID = "freshswitch"
SHIPPER_ORG_ID = "shipper-jinju"
OTHER_SHIPPER_ORG_ID = "shipper-sancheong"
FORWARDER_ORG_ID = "forwarder-dongnam"
OTHER_FORWARDER_ORG_ID = "forwarder-sinhang"


def demo_org_type(org_id: str) -> str:
    return next(org.type for org in demo_orgs() if org.id == org_id)


def login_client(org_id: str, client_app=app) -> TestClient:
    client = TestClient(client_app, base_url="https://testserver", headers=SAAS_CSRF)
    response = client.post(
        "/api/auth/demo-login", json={"org_type": demo_org_type(org_id), "org_id": org_id}
    )
    assert response.status_code == 200, response.text
    assert response.json()["active_org"]["org_id"] == org_id
    return client


@pytest.fixture
def saas_login(demo_tenants) -> Callable[[str], TestClient]:
    return login_client


@pytest.fixture
def platform_client(saas_login) -> TestClient:
    return saas_login(PLATFORM_ORG_ID)


@pytest.fixture
def shipper_client(saas_login) -> TestClient:
    return saas_login(SHIPPER_ORG_ID)


@pytest.fixture
def other_shipper_client(saas_login) -> TestClient:
    return saas_login(OTHER_SHIPPER_ORG_ID)


@pytest.fixture
def forwarder_client(saas_login) -> TestClient:
    return saas_login(FORWARDER_ORG_ID)


@pytest.fixture
def other_forwarder_client(saas_login) -> TestClient:
    return saas_login(OTHER_FORWARDER_ORG_ID)


REAL_PLATFORM_EMAIL = "ops@real-platform.example.com"
REAL_PLATFORM_PASSWORD = "real-platform-password"


def create_real_platform(org_id: str = "platform-real") -> str:
    with database.begin() as db:
        create_platform(
            db,
            name="실제 플랫폼",
            email=REAL_PLATFORM_EMAIL,
            user_name="실제 운영",
            password=REAL_PLATFORM_PASSWORD,
            org_id=org_id,
        )
    return org_id


def password_client(client_app, email: str, password: str) -> TestClient:
    client = TestClient(client_app, base_url="https://testserver", headers=SAAS_CSRF)
    response = client.post("/api/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    return client
