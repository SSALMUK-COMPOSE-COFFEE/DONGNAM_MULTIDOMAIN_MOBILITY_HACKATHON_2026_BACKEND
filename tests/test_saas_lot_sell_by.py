from datetime import timedelta

from fastapi.testclient import TestClient
from test_saas_api import _lot_body, _scenario


def _own_lot(client: TestClient, shipper_id: str):
    lot = next(item for item in _scenario().lots if item.shipper_id == shipper_id)
    response = client.post("/api/saas/shipper/lots", json=_lot_body(lot))
    assert response.status_code == 201, response.text
    return lot, response.json()["registered"]


def test_shipper_lot_ready_date_past_sell_by_recomputes_sell_by(saas_login) -> None:
    jinju = saas_login("shipper-jinju")
    lot, registered = _own_lot(jinju, "sh-jinju")
    product = next(p for p in _scenario().products if p.id == lot.product_id)
    ready = lot.sell_by_date + timedelta(days=3)

    response = jinju.patch(
        f"/api/saas/shipper/lots/{registered['id']}", json={"ready_date": ready.isoformat()}
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ready_date"] == ready.isoformat()
    assert body["sell_by_date"] == (ready + timedelta(days=product.shelf_life_days)).isoformat()


def test_shipper_lot_explicit_sell_by_before_ready_still_rejected(saas_login) -> None:
    jinju = saas_login("shipper-jinju")
    lot, registered = _own_lot(jinju, "sh-jinju")
    ready = lot.sell_by_date + timedelta(days=3)

    response = jinju.patch(
        f"/api/saas/shipper/lots/{registered['id']}",
        json={"ready_date": ready.isoformat(), "sell_by_date": lot.sell_by_date.isoformat()},
    )

    assert response.status_code == 422
    assert "판매기한" in response.text


def test_shipper_lot_pallet_only_patch_keeps_sell_by(saas_login) -> None:
    jinju = saas_login("shipper-jinju")
    lot, registered = _own_lot(jinju, "sh-jinju")

    response = jinju.patch(f"/api/saas/shipper/lots/{registered['id']}", json={"pallets": 1})

    assert response.status_code == 200, response.text
    assert response.json()["sell_by_date"] == lot.sell_by_date.isoformat()


def test_demo_lot_patch_keeps_old_sell_by_rule(client: TestClient) -> None:
    lot = _scenario().lots[0]
    created = client.post(
        "/api/lots", json={**_lot_body(lot), "shipper": lot.shipper_id, "lot_id": "lot-r3-demo"}
    )
    assert created.status_code == 201, created.text
    lot_id = created.json()["registered"]["id"]
    ready = lot.sell_by_date + timedelta(days=3)

    response = client.patch(f"/api/lots/{lot_id}", json={"ready_date": ready.isoformat()})

    assert response.status_code == 422
