from fastapi.testclient import TestClient

from freshswitch_backend.domain.models import Scenario

FIRST_STRAWBERRY_LOTS = {"lot-jinju-sb-1", "lot-sancheong-sb-1", "lot-miryang-sb-1"}
GRAPE_LOTS = {"lot-sangju-gr-1", "lot-pohang-gr-1"}


def _default(client: TestClient) -> Scenario:
    response = client.get("/api/scenarios/default")
    assert response.status_code == 200
    return Scenario.model_validate(response.json())


def test_list_scenarios_includes_default(client: TestClient) -> None:
    response = client.get("/api/scenarios")
    assert response.status_code == 200
    ids = [s["id"] for s in response.json()]
    assert "default" in ids


def test_default_strawberry_lots_fill_ca01(client: TestClient) -> None:
    scenario = _default(client)
    lots = {lot.id: lot for lot in scenario.lots}
    assert sum(lots[i].pallets for i in FIRST_STRAWBERRY_LOTS) == 17
    later = lots["lot-miryang-sb-2"]
    assert later.product_id == "strawberry"
    assert later.ready_date > lots["lot-miryang-sb-1"].ready_date


def test_default_grape_lots(client: TestClient) -> None:
    scenario = _default(client)
    lots = {lot.id: lot for lot in scenario.lots}
    assert sum(lots[i].pallets for i in GRAPE_LOTS) == 18
    products = {p.id: p for p in scenario.products}
    assert products["grape_so2"].so2_treated


def test_default_references_are_consistent(client: TestClient) -> None:
    scenario = _default(client)
    packhouse_ids = {p.id for p in scenario.packhouses}
    shipper_ids = {s.id for s in scenario.shippers}
    product_ids = {p.id for p in scenario.products}
    voyage_ids = {v.id for v in scenario.voyages}
    assert all(s.packhouse_id in packhouse_ids for s in scenario.shippers)
    assert all(lot.shipper_id in shipper_ids for lot in scenario.lots)
    assert all(lot.product_id in product_ids for lot in scenario.lots)
    assert all(c.voyage_id in voyage_ids for c in scenario.containers)
    assert all(v.cy_cutoff < v.etd for v in scenario.voyages)
    assert {"CA-01", "CA-02"} <= {c.id for c in scenario.containers}
    assert len(scenario.voyages) >= 2


def test_unknown_scenario_returns_404(client: TestClient) -> None:
    response = client.get("/api/scenarios/does-not-exist")
    assert response.status_code == 404
