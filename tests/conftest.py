from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from freshswitch_backend.api.vessels import ulsan_client_factory
from freshswitch_backend.main import app
from freshswitch_backend.planning.store import store

FROZEN_NOW = datetime(2026, 11, 10, 0, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def frozen_clock():
    original = store.clock
    store.clock = lambda: FROZEN_NOW
    yield
    store.clock = original


@pytest.fixture(autouse=True)
def no_live_ulsan():
    app.dependency_overrides[ulsan_client_factory] = lambda: None
    yield
    app.dependency_overrides.pop(ulsan_client_factory, None)


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def reset_admin_headers(monkeypatch):
    from freshswitch_backend.api import access

    settings = access.get_settings().model_copy(
        update={"demo_reset_token": SecretStr("test-reset-token")}
    )
    monkeypatch.setattr(access, "get_settings", lambda: settings)
    return {"Authorization": "Bearer test-reset-token"}
