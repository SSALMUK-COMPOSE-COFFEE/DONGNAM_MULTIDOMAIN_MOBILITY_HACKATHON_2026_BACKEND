import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from openai import APIConnectionError

from freshswitch_backend import observability, readiness
from freshswitch_backend.config import Settings
from freshswitch_backend.data.loader import get_scenario
from freshswitch_backend.data.provenance import provenance_catalog, source_kind
from freshswitch_backend.explain import ExplainContext, explain
from freshswitch_backend.readiness import scaled_scenario, workflow_evaluation


@pytest.fixture
def events(monkeypatch):
    captured = []
    monkeypatch.setattr(
        observability.logger, "info", lambda line: captured.append(json.loads(line))
    )
    return captured


def test_complete_workflow_with_rejection_expiry_and_repeated_arrival():
    result = workflow_evaluation()
    assert result["passed"], result
    assert len(result["checks"]) == 4


def test_scale_fixture_has_unique_consistent_ids_without_mutating_original():
    original = get_scenario("default").model_dump(mode="json")
    scaled = scaled_scenario(3)
    assert len(scaled.lots) == 3 * len(original["lots"])
    assert len({lot.id for lot in scaled.lots}) == len(scaled.lots)
    assert {lot.shipper_id for lot in scaled.lots} <= {s.id for s in scaled.shippers}
    assert {c.voyage_id for c in scaled.containers} <= {v.id for v in scaled.voyages}
    assert len({t.id for t in scaled.trucks}) == len(scaled.trucks)
    assert get_scenario("default").model_dump(mode="json") == original
    with pytest.raises(ValueError):
        scaled_scenario(0)


def test_incomplete_initial_plan_cannot_pass_load_evaluation(client, monkeypatch):
    plan_id = client.post("/api/plans", json={"scenario_id": "default"}).json()["plan_id"]
    from freshswitch_backend.planning.store import store

    stored = store.get(plan_id)
    monkeypatch.setattr(
        readiness,
        "build_plan",
        lambda *args, **kwargs: SimpleNamespace(
            plan=stored.plan.model_copy(update={"status": "UNKNOWN"}), routes=stored.routes
        ),
    )

    def forbidden(*args, **kwargs):
        pytest.fail("Cannot evaluate replan on an unknown initial solution")

    monkeypatch.setattr(readiness, "replan", forbidden)
    result = readiness.scalability_evaluation([1])
    assert not result["passed"]
    assert result["cases"][0]["status"] == "no_complete_initial_plan"


def test_provenance_preserves_assumptions_and_does_not_claim_verification(client):
    assert source_kind("가정: JLST 범위에서 환산") == "assumption_or_derived"
    assert source_kind("논문 실증(모의)") == "assumption_or_derived"
    assert source_kind("제안서 정산표") == "demo_or_business_rule"
    assert source_kind("JLST 2025 실측 상한") == "cited_unverified"
    assert source_kind("") == "missing"
    response = client.get("/api/data/provenance")
    assert response.status_code == 200
    catalog = response.json()
    assert catalog["counts"]["assumption_or_derived"] > 0
    assert all(e["verification_status"] == "not_independently_verified" for e in catalog["entries"])
    seed = next(a for a in catalog["assets"] if a["path"] == "quality/seed-observations.json")
    assert seed["kind"] == "simulation"
    assert "실제 도착 검수 기록이 아니다" in seed["metadata"]["label"]
    assert len(seed["sha256"]) == 64
    assert client.get("/api/data/provenance?scenario_id=missing").status_code == 404
    assert provenance_catalog("default")["assets"] == catalog["assets"]


def test_request_id_correlates_http_replan_and_explanation(client, events):
    plan = client.post("/api/plans", json={"scenario_id": "default"}).json()
    response = client.post(
        f"/api/plans/{plan['plan_id']}/events",
        headers={"X-Request-ID": "demo-request-123"},
        json={"event": {"type": "reduce", "lot_id": "lot-sancheong-sb-1", "new_pallets": 3}},
    )
    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == "demo-request-123"
    matching = [e for e in events if e["request_id"] == "demo-request-123"]
    assert {e["event"] for e in matching} == {"http.request", "ai.replan", "ai.explanation"}
    assert (
        next(e for e in matching if e["event"] == "ai.explanation")["fallback_reason"] == "disabled"
    )


def test_audit_omits_secrets_and_sanitizes_incoming_ids(client, events):
    secret = "DO_NOT_LOG_PRIVATE_CREDENTIAL"
    response = client.get(
        "/api/health",
        params={"api_key": secret},
        headers={"Authorization": f"Bearer {secret}", "X-Request-ID": "bad=id"},
    )
    assert response.status_code == 200
    assert response.headers["X-Request-ID"] != "bad=id"
    observability.record_event("test", api_key=secret, body=secret)
    assert secret not in json.dumps(events)
    assert "bad=id" not in json.dumps(events)
    assert observability.request_id.get() is None


def test_streaming_response_keeps_request_id(client, events):
    response = client.get(
        "/api/coldchain/alerts/stream?timeout=0", headers={"X-Request-ID": "stream-test"}
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["X-Request-ID"] == "stream-test"


def test_provider_errors_cannot_echo_credentials_into_audit(events):
    secret = "sk-PRIVATE-DO-NOT-LOG"

    class BrokenProvider:
        async def create(self, **kwargs):
            raise APIConnectionError(message=secret, request=httpx.Request("POST", "http://test"))

    ctx = ExplainContext(
        event_type="reduce",
        event_summary="변경",
        recommended_alternative_id=None,
        alternatives=[],
        shippers=[],
    )
    settings = Settings(llm_api_key=secret, llm_model_name="fake", _env_file=None)
    result = asyncio.run(
        explain(
            ctx,
            settings=settings,
            client=SimpleNamespace(chat=SimpleNamespace(completions=BrokenProvider())),
        )
    )
    assert result.fallback_reason == "provider_error"
    assert secret not in json.dumps(events)
    assert any(e.get("error_type") == "APIConnectionError" for e in events)
