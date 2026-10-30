import pytest
from fastapi.testclient import TestClient

from freshswitch_backend.main import app


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)
