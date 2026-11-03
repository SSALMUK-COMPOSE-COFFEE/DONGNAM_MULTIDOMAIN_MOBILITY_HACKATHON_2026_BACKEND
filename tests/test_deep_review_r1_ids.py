import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from saas_fixtures import demo_org_type

from freshswitch_backend.config import get_settings
from freshswitch_backend.data.loader import load_scenarios
from freshswitch_backend.domain.ids import ID_MAX_LENGTH, id_error
from freshswitch_backend.intake.models import Resolved
from freshswitch_backend.intake.service import _lot_id
from freshswitch_backend.main import create_app

CSRF = {"X-Requested-With": "freshswitch"}
BAD_IDS = ["x/y", "demo-a/b", "demo a", " ", "-lead", ".lead", "a" * (ID_MAX_LENGTH + 1), "부산"]
FORM = {
    "product": "딸기",
    "pallets": 2,
    "ready_date": "2026-11-17",
    "shipper": "산청",
    "destination": "홍콩",
}
LITERAL = re.compile(
    r"""(?:["'](?:lot_id|device_id)["']\s*:\s*|\b(?:lot_id|device_id|LOT_ID|DEVICE_ID)\s*=\s*)"""
    r"""["']([^"'{}]+)["']"""
)
INTENTIONALLY_INVALID = {"=1+1"}


@pytest.fixture
def saas(demo_tenants):
    return create_app(get_settings().model_copy(update={"saas_mode": True}))


def _platform(app) -> TestClient:
    client = TestClient(app, base_url="https://testserver")
    body = {"org_type": demo_org_type("freshswitch"), "org_id": "freshswitch"}
    assert client.post("/api/auth/demo-login", json=body).status_code == 200
    return client


def test_existing_scenario_and_generated_ids_pass_rule() -> None:
    for scenario in load_scenarios().values():
        for lot in scenario.lots:
            assert id_error(lot.id) is None, lot.id
        for shipper in scenario.shippers:
            for product in scenario.products:
                resolved = Resolved(
                    product_id=product.id,
                    product_name=product.name,
                    shipper_id=shipper.id,
                    shipper_name=shipper.name,
                    packhouse_id="p",
                    packhouse_name="p",
                    destination_port_id="HKHKG",
                    destination_name="홍콩",
                )
                generated = _lot_id(set(), resolved)
                assert id_error(generated) is None, generated


def test_existing_fixture_ids_pass_rule() -> None:
    roots = [
        Path(__file__).parent,
        Path(__file__).parents[1] / "src",
        Path(__file__).parents[1] / "scripts",
    ]
    found: set[str] = set()
    for root in roots:
        for path in root.rglob("*.py"):
            found.update(LITERAL.findall(path.read_text(encoding="utf-8")))
    assert {"lot-jinju-sb-1", "sim-01", "demo-p1", "arduino-01"} <= found
    bad = {value for value in found - INTENTIONALLY_INVALID if id_error(value) is not None}
    assert bad == set()


@pytest.mark.parametrize("bad", BAD_IDS)
def test_intake4_lot_id_rule(client: TestClient, bad: str) -> None:
    response = client.post("/api/intake/lots", json={**FORM, "lot_id": bad})
    assert response.status_code == 422, response.text


def test_intake4_blank_lot_id_is_generated(client: TestClient) -> None:
    response = client.post("/api/intake/lots", json={**FORM, "lot_id": ""})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["lot"]["id"] == "lot-sancheong-strawberry-1"
    assert any(item.startswith("lot_id 자동 생성") for item in body["defaults_applied"])
    explicit = client.post("/api/intake/lots", json={**FORM, "lot_id": "lot-sancheong.v2_1"})
    assert explicit.status_code == 200, explicit.text
    assert explicit.json()["lot"]["id"] == "lot-sancheong.v2_1"


@pytest.mark.parametrize("bad", ["demo-a/b", "demo-a b", "demo-" + "a" * ID_MAX_LENGTH])
def test_coldchain3_device_id_rule(saas, bad: str) -> None:
    platform = _platform(saas)
    created = platform.post("/api/saas/devices", json={"device_id": bad}, headers=CSRF)
    assert created.status_code == 422, created.text
    bound = platform.post(
        "/api/coldchain/bindings", json={"device_id": bad, "container_id": "CA-01"}, headers=CSRF
    )
    assert bound.status_code == 422, bound.text


@pytest.mark.parametrize("bad", ["x/y", "x y", "=1+1"])
def test_coldchain3_reading_and_simulation_device_id_rule(client: TestClient, bad: str) -> None:
    reading = {"device_id": bad, "container_id": "CA-01", "temp_c": 1.0, "power_on": True}
    assert client.post("/api/coldchain/readings", json=reading).status_code == 422
    bound = client.post("/api/coldchain/bindings", json={"device_id": bad, "container_id": "CA-01"})
    assert bound.status_code == 422
    simulated = client.post("/api/coldchain/simulate", json={"device_id": bad})
    assert simulated.status_code == 422


def test_coldchain3_valid_device_can_be_deleted(saas) -> None:
    platform = _platform(saas)
    created = platform.post("/api/saas/devices", json={"device_id": "demo-a.b_c"}, headers=CSRF)
    assert created.status_code == 201, created.text
    deleted = platform.delete("/api/saas/devices/demo-a.b_c", headers=CSRF)
    assert deleted.status_code == 204, deleted.text
