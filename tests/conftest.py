from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

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
