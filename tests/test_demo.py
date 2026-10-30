from fastapi.testclient import TestClient

from freshswitch_backend.api import demo, vessels
from freshswitch_backend.config import Settings
from freshswitch_backend.planning.store import store


def test_reset_clears_sessions(client: TestClient) -> None:
    plan_id = client.post("/api/plans", json={"scenario_id": "default"}).json()["plan_id"]
    assert store.count() >= 1
    response = client.post("/api/demo/reset")
    assert response.status_code == 200
    assert response.json()["cleared_sessions"] >= 1
    assert store.count() == 0
    assert client.get(f"/api/plans/{plan_id}").status_code == 404


def test_status(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(
        demo,
        "get_settings",
        lambda: Settings(
            offline_mode=True,
            explain_use_llm=False,
            llm_api_key="x",
            llm_model_name="m",
            data_go_kr_service_key="",
        ),
    )
    body = client.get("/api/demo/status").json()
    assert body["offline_mode"] is True
    assert body["explain_use_llm"] is False
    assert body["llm_configured"] is True
    assert body["mof_key_configured"] is False
    assert body["demo_fixture"] is True
    assert "default" in body["scenario_ids"]
    assert "2026-10-30" in [d["date"] for d in body["snapshot_dates"]]
    assert all(d["directions"] == ["I", "O"] for d in body["snapshot_dates"])


def test_script_runs_offline_end_to_end(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(vessels, "get_settings", lambda: Settings(offline_mode=True))
    steps = client.get("/api/demo/script").json()
    assert [s["step"] for s in steps] == list(range(1, len(steps) + 1))
    assert [s["id"] for s in steps] == [
        "create_plan",
        "reduce_sancheong",
        "apply_pull_forward",
        "detect_delay",
        "replan_delay",
        "apply_delay_plan",
        "compare_air",
    ]
    plan_id = None
    previous: dict = {}
    for step in steps:
        path = step["path"].replace("{plan_id}", plan_id or "")
        body = step["body"]
        if step["id"] == "replan_delay":
            detected = {k: v for k, v in previous["events"][0].items() if v is not None}
            assert detected == body["event"]
        if step["id"] == "apply_delay_plan":
            assert previous["recommended_id"] == body["alternative_id"]
        if isinstance(body, dict) and body.get("plan_id") == "{plan_id}":
            body = {**body, "plan_id": plan_id}
        response = client.request(step["method"], path, json=body)
        assert response.status_code in (200, 201), (step["id"], response.text)
        previous = response.json()
        if step["id"] == "create_plan":
            plan_id = previous["plan_id"]
    assert previous["lot_id"] == "lot-miryang-sb-2"
