from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from freshswitch_backend.coldchain import ColdchainStore
from freshswitch_backend.domain.models import KST
from freshswitch_backend.lots import LotStore
from freshswitch_backend.main import app
from freshswitch_backend.persistence import Database, database
from freshswitch_backend.planning.store import SessionStore
from freshswitch_backend.quality import QualityHistory

FORM = {
    "product": "딸기",
    "pallets": 2,
    "ready_date": "2026-11-17",
    "shipper": "산청",
    "destination": "홍콩",
}
START = datetime(2026, 11, 17, 9, 0, tzinfo=KST)


def _lot_ids(plan: dict) -> set[str]:
    return {lot["id"] for lot in plan["scenario"]["lots"]}


def test_lot_crud(client: TestClient) -> None:
    created = client.post("/api/lots", json=FORM)
    assert created.status_code == 201
    body = created.json()
    lot = body["registered"]
    assert lot["id"] == "lot-sancheong-strawberry-1"
    assert lot["status"] == "registered"
    assert lot["packhouse"] == body["resolved"]["packhouse_id"]
    assert body["hints"]["based_on"] == "scenario"

    second = client.post("/api/lots", json=FORM).json()["registered"]
    assert second["id"] == "lot-sancheong-strawberry-2"

    listed = client.get("/api/lots", params={"shipper": "sh-sancheong"}).json()
    assert [x["id"] for x in listed] == [lot["id"], second["id"]]
    assert client.get("/api/lots", params={"product": "grape"}).json() == []
    ranged = client.get(
        "/api/lots", params={"ready_from": "2026-11-18", "ready_to": "2026-11-30"}
    ).json()
    assert ranged == []

    assert client.get(f"/api/lots/{lot['id']}").json()["pallets"] == 2
    patched = client.patch(f"/api/lots/{lot['id']}", json={"pallets": 4, "destination": "HKHKG"})
    assert patched.status_code == 200
    assert patched.json()["pallets"] == 4
    assert client.patch(f"/api/lots/{lot['id']}", json={"pallets": 0}).status_code == 422
    assert client.patch(f"/api/lots/{lot['id']}", json={"product": "바나나"}).status_code == 422
    assert client.patch(f"/api/lots/{lot['id']}", json={"bogus": 1}).status_code == 422
    assert client.get("/api/lots", params={"status": "cancelled"}).json() == []

    assert client.delete(f"/api/lots/{lot['id']}").status_code == 204
    assert client.get(f"/api/lots/{lot['id']}").status_code == 404
    assert client.delete(f"/api/lots/{lot['id']}").status_code == 404


def test_duplicate_lot_id_rejected(client: TestClient) -> None:
    client.post("/api/lots", json={**FORM, "lot_id": "lot-x"})
    again = client.post("/api/lots", json={**FORM, "lot_id": "lot-x"})
    assert again.status_code == 422


def test_intake_preview_does_not_save_unless_asked(client: TestClient) -> None:
    preview = client.post("/api/intake/lots", json=FORM).json()
    assert preview["registered"] is None
    assert client.get("/api/lots").json() == []
    saved = client.post("/api/intake/lots", json={**FORM, "save": True}).json()
    assert saved["registered"]["id"] == preview["lot"]["id"]
    assert len(client.get("/api/lots").json()) == 1


def test_plan_includes_registered_and_inline_lots(client: TestClient) -> None:
    lot = client.post("/api/lots", json=FORM).json()["registered"]
    base = client.post("/api/plans", json={}).json()
    assert lot["id"] not in _lot_ids(base)

    by_id = client.post("/api/plans", json={"lot_ids": [lot["id"], lot["id"]]})
    assert by_id.status_code == 201
    plan = by_id.json()
    assert lot["id"] in _lot_ids(plan)
    assert len(plan["scenario"]["lots"]) == len(base["scenario"]["lots"]) + 1
    assigned = {a["lot_id"] for a in plan["plan"]["assignments"]}
    unassigned = {u["lot_id"] for u in plan["plan"]["unassigned"]}
    assert lot["id"] in assigned | unassigned
    assert client.get(f"/api/lots/{lot['id']}").json()["status"] == "planned"

    extra = client.post("/api/lots", json={**FORM, "pallets": 3}).json()["registered"]
    included = client.post("/api/plans", json={"include_registered": True}).json()
    assert extra["id"] in _lot_ids(included)
    assert lot["id"] not in _lot_ids(included)

    inline = {**plan["scenario"]["lots"][0], "id": "lot-inline-1"}
    with_inline = client.post("/api/plans", json={"lots": [inline]}).json()
    assert "lot-inline-1" in _lot_ids(with_inline)

    unknown = client.post("/api/plans", json={"lot_ids": ["nope"]})
    assert unknown.status_code == 422
    bad = client.post("/api/plans", json={"lots": [{**inline, "id": "lot-bad", "product_id": "x"}]})
    assert bad.status_code == 422


def test_plan_list_and_delete(client: TestClient) -> None:
    first = client.post("/api/plans", json={}).json()["plan_id"]
    second = client.post("/api/plans", json={}).json()["plan_id"]
    listed = client.get("/api/plans").json()
    assert [p["plan_id"] for p in listed] == [second, first]
    summary = listed[0]
    assert summary["version"] == 1
    assert summary["status"] == "committed"
    assert summary["lot_count"] > 0
    assert "total_savings" in summary["totals"]
    assert client.delete(f"/api/plans/{first}").status_code == 204
    assert client.get(f"/api/plans/{first}").status_code == 404
    assert client.delete(f"/api/plans/{first}").status_code == 404
    assert [p["plan_id"] for p in client.get("/api/plans").json()] == [second]


def test_quality_observation_get_delete(client: TestClient) -> None:
    observations = client.get("/api/quality/observations").json()["observations"]
    target = observations[0]["id"]
    assert client.get(f"/api/quality/observations/{target}").json()["id"] == target
    assert client.delete(f"/api/quality/observations/{target}").status_code == 204
    assert client.get(f"/api/quality/observations/{target}").status_code == 404
    count = client.get("/api/quality/observations").json()["count"]
    assert count == len(observations) - 1


def test_demo_reset_keeps_lots_unless_asked(client: TestClient, reset_admin_headers) -> None:
    client.post("/api/lots", json=FORM)
    client.post("/api/plans", json={})
    kept = client.post("/api/demo/reset", headers=reset_admin_headers).json()
    assert kept["cleared_lots"] == 0
    assert len(client.get("/api/lots").json()) == 1
    assert client.get("/api/plans").json() == []
    wiped = client.post(
        "/api/demo/reset", params={"include_lots": True}, headers=reset_admin_headers
    ).json()
    assert wiped["cleared_lots"] == 1
    assert client.get("/api/lots").json() == []


def test_health_reports_db(client: TestClient) -> None:
    body = client.get("/api/health").json()
    assert body["db"] == "ok"


def test_health_503_when_db_down(postgres: str) -> None:
    down = Database("postgresql://nobody:nothing@127.0.0.1:1/none")
    original = database._url
    database.configure("postgresql://nobody:nothing@127.0.0.1:1/none")
    try:
        response = TestClient(app).get("/api/health")
        assert response.status_code == 503
        assert response.json()["db"] == "error"
        assert not down.ping()
    finally:
        database.configure(original or postgres)


def test_state_survives_new_store_instances(client: TestClient) -> None:
    plan_id = client.post("/api/plans", json={}).json()["plan_id"]
    client.post("/api/lots", json=FORM)
    readings = [
        {
            "device_id": "dev-1",
            "container_id": "CA-01",
            "ts": (START + timedelta(minutes=m)).isoformat(),
            "temp_c": 1.0,
            "power_on": m < 30,
        }
        for m in range(0, 120, 5)
    ]
    ingest = client.post("/api/coldchain/readings", json=readings).json()
    assert ingest["accepted"] == len(readings)
    alerts = ingest["new_alerts"]
    observation = client.post(
        "/api/quality/observations",
        json={
            "product_id": "strawberry",
            "destination": "HKHKG",
            "mode": "sea",
            "pallets": 2,
            "wait_days": 1,
            "actual_loss_pct": 3.0,
            "source": "test",
        },
    ).json()

    fresh_db = Database(database._url)
    plans = SessionStore(db=fresh_db)
    assert plans.get(plan_id).plan_id == plan_id
    assert [s.plan_id for s in plans.summaries()] == [plan_id]
    assert len(LotStore(db=fresh_db).ids()) == 1
    twin = ColdchainStore(db=fresh_db).state("CA-01")
    assert twin.has_data
    assert twin.reading_count == len(readings)
    assert [a.seq for a in twin.alerts] == [a["seq"] for a in alerts]
    history = QualityHistory(db=fresh_db)
    assert history.get(observation["id"]) is not None
    fresh_db.configure(None)
