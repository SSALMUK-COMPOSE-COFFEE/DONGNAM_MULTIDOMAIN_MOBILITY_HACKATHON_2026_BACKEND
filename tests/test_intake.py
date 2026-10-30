from fastapi.testclient import TestClient

FORM = {
    "product": "딸기",
    "pallets": 2,
    "ready_date": "2026-11-17",
    "shipper": "산청",
    "destination": "홍콩",
}


def _plan(client: TestClient) -> str:
    return client.post("/api/plans", json={"scenario_id": "default"}).json()["plan_id"]


def test_intake_resolves_simple_form_and_defaults_sell_by(client: TestClient) -> None:
    response = client.post("/api/intake/lots", json=FORM)
    assert response.status_code == 200
    body = response.json()
    lot = body["lot"]
    assert lot["shipper_id"] == "sh-sancheong"
    assert lot["product_id"] == "strawberry"
    assert lot["destination"] == "HKHKG"
    assert lot["sell_by_date"] == "2026-11-24"
    assert lot["id"] == "lot-sancheong-strawberry-1"
    assert body["event"] == {"type": "add_lot", "lot": lot}
    assert any("sell_by_date" in d for d in body["defaults_applied"])
    assert body["hints"]["based_on"] == "scenario"


def test_intake_hints_against_plan(client: TestClient) -> None:
    plan_id = _plan(client)
    body = client.post("/api/intake/lots", json={**FORM, "plan_id": plan_id}).json()
    options = {o["container_id"]: o for o in body["hints"]["sea_options"]}
    assert options["CA-01"]["feasible"] is True
    assert options["CA-01"]["free_pallets"] == 3
    assert options["CA-02"]["feasible"] is False
    assert "SO2" in options["CA-02"]["reasons"][0]
    assert body["hints"]["earliest_feasible"]["container_id"] == "CA-01"
    assert body["hints"]["air"]["feasible"] is True


def test_intake_so2_warning_and_separate_container(client: TestClient) -> None:
    plan_id = _plan(client)
    form = {
        "product": "grape_so2",
        "pallets": 3,
        "ready_date": "2026-11-16",
        "shipper": "ph-pohang",
        "destination": "HKHKG",
        "plan_id": plan_id,
    }
    hints = client.post("/api/intake/lots", json=form).json()["hints"]
    assert any("SO2" in w and "별도" in w for w in hints["warnings"])
    options = {o["container_id"]: o for o in hints["sea_options"]}
    assert options["CA-01"]["feasible"] is False
    assert options["CA-02"]["reasons"] == ["남은 자리 2파렛트 < 3파렛트"]
    assert hints["earliest_feasible"]["container_id"] == "CA-03"


def test_intake_pull_forward_warning(client: TestClient) -> None:
    plan_id = _plan(client)
    form = {**FORM, "ready_date": "2026-11-18", "plan_id": plan_id}
    hints = client.post("/api/intake/lots", json=form).json()["hints"]
    assert hints["earliest_feasible"]["container_id"] == "CA-01"
    assert hints["earliest_feasible"]["pull_forward_days"] == 1
    assert any("1일 앞당겨야" in w for w in hints["warnings"])


def test_intake_validation_errors(client: TestClient) -> None:
    form = {
        "product": "사과",
        "pallets": 30,
        "ready_date": "2026-11-17",
        "shipper": "부산",
        "destination": "도쿄",
    }
    response = client.post("/api/intake/lots", json=form)
    assert response.status_code == 422
    fields = {e["field"] for e in response.json()["detail"]}
    assert fields == {"product", "pallets", "shipper", "destination"}
    bad_date = client.post(
        "/api/intake/lots", json={**FORM, "sell_by_date": "2026-11-10", "pallets": 0}
    )
    assert {e["field"] for e in bad_date.json()["detail"]} == {"sell_by_date", "pallets"}
    duplicate = client.post("/api/intake/lots", json={**FORM, "lot_id": "lot-jinju-sb-1"})
    assert duplicate.json()["detail"][0]["field"] == "lot_id"
    assert client.post("/api/intake/lots", json={**FORM, "plan_id": "nope"}).status_code == 404


def test_intake_event_feeds_replan(client: TestClient) -> None:
    plan_id = _plan(client)
    event = client.post("/api/intake/lots", json={**FORM, "plan_id": plan_id}).json()["event"]
    response = client.post(f"/api/plans/{plan_id}/events", json={"event": event})
    assert response.status_code == 200
    assert response.json()["event"]["lot"]["id"] == "lot-sancheong-strawberry-1"
