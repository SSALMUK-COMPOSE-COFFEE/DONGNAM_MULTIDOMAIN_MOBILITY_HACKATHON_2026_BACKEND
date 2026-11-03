from fastapi.testclient import TestClient
from saas_fixtures import (
    PLATFORM_ORG_ID,
    REAL_PLATFORM_EMAIL,
    REAL_PLATFORM_PASSWORD,
    create_real_platform,
    password_client,
)

from freshswitch_backend.analysis.emissions import AIR_G_PER_TKM, SEA_G_PER_TKM
from freshswitch_backend.data.loader import get_scenario
from freshswitch_backend.main import app
from freshswitch_backend.persistence.seed import shipper_org_id
from freshswitch_backend.planning.store import store

MONTH = "2026-11"
GRAPE = "grape_so2"


def _scenario():
    scenario = get_scenario("default")
    assert scenario is not None
    return scenario


def _register_all(saas_login) -> list[str]:
    ids = []
    for lot in _scenario().lots:
        client = saas_login(shipper_org_id(lot.shipper_id))
        response = client.post(
            "/api/saas/shipper/lots",
            json={
                "product": lot.product_id,
                "pallets": lot.pallets,
                "ready_date": lot.ready_date.isoformat(),
                "destination": lot.destination,
                "sell_by_date": lot.sell_by_date.isoformat(),
                "sell_window": lot.sell_window,
                "lot_id": lot.id,
            },
        )
        assert response.status_code == 201, response.text
        ids.append(response.json()["registered"]["id"])
    return ids


def _platform_plan(platform_client: TestClient, saas_login) -> dict:
    response = platform_client.post(
        "/api/saas/platform/plans", json={"lot_ids": _register_all(saas_login)}
    )
    assert response.status_code == 201, response.text
    return response.json()


def _metrics(client: TestClient, month: str = MONTH) -> dict:
    response = client.get("/api/saas/platform/metrics", params={"month": month})
    assert response.status_code == 200, response.text
    return response.json()


def test_metrics_match_plan(platform_client: TestClient, saas_login) -> None:
    session = _platform_plan(platform_client, saas_login)
    plan = session["plan"]
    body = _metrics(platform_client)
    assert body["month"] == MONTH
    assert body["plan_count"] == 1
    assert body["shipment_count"] == len(plan["assignments"])
    assert body["shipment_pallets"] == sum(a["pallets"] for a in plan["assignments"])
    sea = [a for a in plan["assignments"] if a["mode"] == "sea"]
    assert body["sea_pallets"] == sum(a["pallets"] for a in sea)
    used = [c for c in plan["containers"] if c["used"]]
    assert body["reefer_containers"] == len(used)
    fill = sum(c["pallets"] for c in used) / sum(c["capacity_pallets"] for c in used)
    assert body["average_fill"]["value"] == round(fill, 4)
    settlement = platform_client.get("/api/saas/platform/settlements").json()[0]
    assert body["shipper_savings_krw"] == settlement["total_saving_krw"]
    assert body["platform_fee_krw"] == settlement["platform_fee_krw"]
    assert body["performance_fee_krw"] == settlement["performance_fee_krw"]
    assert body["usage_fee_krw"] == settlement["usage_fee_krw"]
    assert body["performance_fee_krw"] > 0 and body["usage_fee_krw"] > 0
    avoided = sum(a["pallets"] for a in sea if a["product_id"] != GRAPE)
    assert avoided > 0
    assert body["air_avoided_pallets"] == avoided
    co2 = body["co2"]
    assert co2["air_avoided_pallets"] == avoided
    tonnes = avoided * 415 / 1000
    assert co2["lines"][0]["tonnes_cargo"] == round(tonnes, 3)
    assert 0 < co2["saved_t_co2"] < tonnes * 2_060 * AIR_G_PER_TKM / 1e6
    assert co2["saved_t_co2"] > tonnes * 2_060 * (AIR_G_PER_TKM - SEA_G_PER_TKM) / 1e6 * 0.9
    assert "IEA" in co2["factor_source"]
    assert body["plan_confirmation_rate"]["value"] == 1.0
    assert body["order_confirmation_rate"]["value"] is None
    assert body["order_confirmation_rate"]["reason"]
    assert body["average_replan_ms"]["value"] is None
    assert body["average_replan_ms"]["reason"]
    assert body["average_arrival_loss_gap"]["value"] is None
    assert body["shipper_reuse_rate"]["value"] == 0.0
    regions = {r["region"]: r for r in body["regions"]}
    assert set(regions) == {"부울경", "경북"}
    assert sum(r["savings_krw"] for r in regions.values()) == body["shipper_savings_krw"]
    assert sum(r["pallets"] for r in regions.values()) == body["shipment_pallets"]
    assert all(r["reefer_containers"] >= 1 for r in regions.values())


def test_metrics_reuse_and_month(platform_client: TestClient, saas_login) -> None:
    _platform_plan(platform_client, saas_login)
    scenario = _scenario()
    subset = scenario.model_copy(update={"lots": scenario.lots[:2]})
    store.create(subset, org_id=PLATFORM_ORG_ID)
    body = _metrics(platform_client)
    assert body["plan_count"] == 2
    repeat = {lot.shipper_id for lot in scenario.lots[:2]}
    total = {lot.shipper_id for lot in scenario.lots}
    assert body["shipper_reuse_rate"]["numerator"] == len(repeat)
    assert body["shipper_reuse_rate"]["denominator"] == len(total)
    empty = _metrics(platform_client, "2026-10")
    assert empty["plan_count"] == 0
    assert empty["average_fill"]["value"] is None
    assert empty["average_fill"]["reason"]
    assert empty["co2"]["saved_t_co2"] == 0
    assert empty["regions"] == []


def test_metrics_exclude_other_orgs(client: TestClient, platform_client: TestClient, saas_login):
    _platform_plan(platform_client, saas_login)
    assert client.post("/api/plans", json={}).status_code == 201
    real = create_real_platform()
    store.create(_scenario(), org_id=real)
    store.create(_scenario(), org_id=real)
    assert _metrics(platform_client)["plan_count"] == 1
    other = password_client(app, REAL_PLATFORM_EMAIL, REAL_PLATFORM_PASSWORD)
    assert _metrics(other)["plan_count"] == 2


def test_metrics_roles(
    client: TestClient, shipper_client: TestClient, forwarder_client: TestClient
) -> None:
    for path in ("metrics", "business", "business/years"):
        assert client.get(f"/api/saas/platform/{path}").status_code == 401
        assert shipper_client.get(f"/api/saas/platform/{path}").status_code == 403
        assert forwarder_client.get(f"/api/saas/platform/{path}").status_code == 403


def test_metrics_month_validation(platform_client: TestClient) -> None:
    response = platform_client.get("/api/saas/platform/metrics", params={"month": "2026-13"})
    assert response.status_code == 422
    default = platform_client.get("/api/saas/platform/metrics")
    assert default.status_code == 200
    assert default.json()["month"] == MONTH
