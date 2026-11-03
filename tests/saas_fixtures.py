from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient

from freshswitch_backend.main import app
from freshswitch_backend.persistence.seed import DEMO_EMAIL_DOMAIN

SAAS_CSRF = {"X-Requested-With": "freshswitch"}
SAAS_DEMO_PASSWORD = "test-demo-password"
PLATFORM_ORG_ID = "freshswitch"
SHIPPER_ORG_ID = "shipper-jinju"
OTHER_SHIPPER_ORG_ID = "shipper-sancheong"
FORWARDER_ORG_ID = "forwarder-dongnam"
OTHER_FORWARDER_ORG_ID = "forwarder-sinhang"


def login_client(org_id: str, client_app=app) -> TestClient:
    client = TestClient(client_app, base_url="https://testserver", headers=SAAS_CSRF)
    response = client.post(
        "/api/auth/login",
        json={"email": f"{org_id}@{DEMO_EMAIL_DOMAIN}", "password": SAAS_DEMO_PASSWORD},
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
