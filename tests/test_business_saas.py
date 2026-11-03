from fastapi.testclient import TestClient
from saas_fixtures import PLATFORM_ORG_ID
from test_metrics import _platform_plan, _scenario

from freshswitch_backend.analysis import business_matrix
from freshswitch_backend.analysis.business import (
    ANNUAL_FIXED_COST,
    DEFAULT_OPS_COST_PER_VOYAGE,
    MVP_CASH_FIXED_COST,
    voyage_economics,
)
from freshswitch_backend.planning.store import store


def _years(client: TestClient, **params) -> dict:
    response = client.get("/api/saas/platform/business/years", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def _line(body: dict, share: float, mode: str, level: str) -> dict:
    return next(
        line
        for line in body["lines"]
        if line["forwarder_share"] == share
        and line["base_fee_mode"] == mode
        and line["fixed_level"] == level
    )


def test_years_first_year_matches_matrix(platform_client: TestClient) -> None:
    body = _years(platform_client)
    assert body["years"] == 5
    assert len(body["lines"]) == 8
    matrix = business_matrix(_scenario())
    assert matrix is not None
    for share in (0.0, 0.4):
        for level in ("full", "mvp_cash"):
            row = next(
                r
                for r in matrix.rows
                if r.fee_rate == 0.25
                and r.base_fee_per_pallet == 0
                and r.air_regime == "normal"
                and r.forwarder_share == share
                and r.fixed_level == level
            )
            for mode in ("deducted", "additive"):
                first = _line(body, share, mode, level)["rows"][0]
                assert first["revenue"] == row.revenue
                assert first["forwarder_cost"] == row.forwarder_cost
                assert first["net_before_subsidy"] == row.net
                assert first["fixed_cost"] == row.fixed_cost


def test_years_rows_add_up(platform_client: TestClient) -> None:
    body = _years(platform_client)
    volumes = body["volumes"]
    assert volumes[0]["tonnes"] == {"strawberry": 109.0, "grape_so2": 298.0}
    assert volumes[4]["tonnes"] == {"strawberry": 1316.0, "grape_so2": 1990.0}
    assert volumes[4]["pallets"]["strawberry"] == round(1_316_000 / 415)
    assert "가정" in volumes[2]["source"]
    assert all("확인 필요" in s["source"] for s in body["subsidies"])
    deducted = _line(body, 0.0, "deducted", "full")
    additive = _line(body, 0.0, "additive", "full")
    assert additive["rows"][1]["revenue"] > deducted["rows"][1]["revenue"]
    strawberry = volumes[1]["pallets"]["strawberry"]
    assert additive["rows"][1]["revenue"] - deducted["rows"][1]["revenue"] == strawberry * 20_000
    cumulative = 0
    for row, subsidy in zip(deducted["rows"], body["subsidies"], strict=True):
        assert row["revenue"] == row["performance_fee"] + row["usage_fee"]
        assert row["contribution"] == row["revenue"] - row["forwarder_cost"]
        assert row["net_before_subsidy"] == row["contribution"] - ANNUAL_FIXED_COST
        assert row["net_after_subsidy"] == row["net_before_subsidy"] + subsidy["amount"]
        cumulative += row["net_before_subsidy"]
        assert row["cumulative_before_subsidy"] == cumulative
    assert deducted["rows"][4]["excluded_upside"] == 190_000_000
    sensitivity = _line(body, 0.4, "deducted", "full")
    for base, risky in zip(deducted["rows"], sensitivity["rows"], strict=True):
        assert risky["forwarder_cost"] == round(base["revenue"] * 0.4)
    mvp = _line(body, 0.0, "deducted", "mvp_cash")
    assert mvp["rows"][0]["fixed_cost"] == MVP_CASH_FIXED_COST
    assert mvp["breakeven_year_before_subsidy"] == 1
    nets = [r["net_before_subsidy"] for r in deducted["rows"]]
    expected = next((i + 1 for i, n in enumerate(nets) if n >= 0), None)
    assert deducted["breakeven_year_before_subsidy"] == expected


def test_years_query_overrides(platform_client: TestClient) -> None:
    body = _years(
        platform_client,
        years=3,
        subsidies=[5, 6],
        fixed_cost=1_000,
        volume_scale=2,
        sensitivity_forwarder_share=0.3,
    )
    assert body["years"] == 3
    assert [s["amount"] for s in body["subsidies"]] == [5, 6, 0]
    assert body["volumes"][0]["tonnes"]["strawberry"] == 218.0
    line = _line(body, 0.3, "deducted", "full")
    assert line["rows"][0]["fixed_cost"] == 1_000
    assert line["breakeven_year_before_subsidy"] == 1
    bad = platform_client.get("/api/saas/platform/business/years", params={"subsidies": [-1]})
    assert bad.status_code == 422


def test_platform_business_uses_org_plans(platform_client: TestClient, saas_login) -> None:
    session = _platform_plan(platform_client, saas_login)
    stored = store.get(session["plan_id"], org_id=PLATFORM_ORG_ID)
    assert stored is not None
    response = platform_client.get("/api/saas/platform/business", params={"month": "2026-11"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["plan_count"] == 1
    base, risky = body["lines"]
    expected = voyage_economics(stored.scenario, stored.plan, 0.0, DEFAULT_OPS_COST_PER_VOYAGE)
    assert base["fee_revenue"] == sum(v.fee_revenue for v in expected)
    assert base["fee_revenue"] > 0
    assert base["forwarder_cost"] == 0
    assert base["voyage_count"] == len(expected)
    assert base["fixed_cost_allocation"] == DEFAULT_OPS_COST_PER_VOYAGE * len(expected)
    assert base["platform_net"] == base["contribution_margin"] - base["fixed_cost_allocation"]
    assert base["annual_contribution"] == round(base["contribution_margin"] / len(expected) * 52)
    assert risky["forwarder_share"] == 0.4
    assert risky["forwarder_cost"] == sum(
        v.forwarder_share
        for v in voyage_economics(stored.scenario, stored.plan, 0.4, DEFAULT_OPS_COST_PER_VOYAGE)
    )
    used = [c for c in stored.plan.containers if c.used]
    assert body["empty_slot_cost"] == sum(
        round(c.container_cost * (c.capacity_pallets - c.pallets) / c.capacity_pallets)
        for c in used
    )
    assert body["tariff_operational_gap"] == (
        stored.plan.total_shared_cost - stored.plan.total_operational_cost
    )
    empty = platform_client.get("/api/saas/platform/business", params={"month": "2026-01"})
    assert empty.json()["plan_count"] == 0
    assert empty.json()["lines"][0]["annual_net"] is None
