from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient
from test_saas_api import _lot_body

from freshswitch_backend.data.loader import get_scenario
from freshswitch_backend.domain.models import Scenario
from freshswitch_backend.optimize import AIR, solve_plan
from freshswitch_backend.persistence.seed import shipper_org_id
from freshswitch_backend.planning.service import CUTOFF_REASON, build_plan

LATE_LOT = "lot-miryang-sb-2"


@pytest.fixture(scope="module")
def scenario() -> Scenario:
    found = get_scenario("default")
    assert found is not None
    return found


def _with_lot(scenario: Scenario, lot_id: str, **update) -> Scenario:
    lots = [lot.model_copy(update=update) if lot.id == lot_id else lot for lot in scenario.lots]
    return scenario.model_copy(update={"lots": lots}, deep=True)


def _late(scenario: Scenario) -> Scenario:
    return _with_lot(scenario, LATE_LOT, ready_date=date(2026, 11, 21))


def test_sea_assignments_have_no_reason_and_air_has_code(scenario: Scenario) -> None:
    plan = build_plan(scenario.model_copy(deep=True)).plan
    sea = [a for a in plan.assignments if a.mode == "sea"]
    air = [a for a in plan.assignments if a.mode == "air"]
    assert sea and air
    assert all(a.reason is None for a in sea)
    by_lot = {a.lot_id: a for a in air}
    assert by_lot[LATE_LOT].reason is not None
    assert by_lot[LATE_LOT].reason.code == "capacity_or_cost"
    assert by_lot[LATE_LOT].reason.message


def test_air_after_every_cutoff_is_sea_deadline_miss(scenario: Scenario) -> None:
    plan = build_plan(_late(scenario)).plan
    late = next(a for a in plan.assignments if a.lot_id == LATE_LOT)
    assert late.mode == "air"
    assert late.reason is not None
    assert late.reason.code == "sea_deadline_miss"
    for container in scenario.containers:
        assert container.id in late.reason.message
    assert "선적 마감" in late.reason.message


def test_fixed_air_with_sea_options_is_fixed(scenario: Scenario) -> None:
    plan = solve_plan(scenario, fixed={"lot-jinju-sb-1": AIR})
    jinju = next(a for a in plan.assignments if a.lot_id == "lot-jinju-sb-1")
    assert jinju.mode == "air"
    assert jinju.reason is not None and jinju.reason.code == "fixed"


def test_forbidden_sea_is_sea_forbidden(scenario: Scenario) -> None:
    forbidden = [("lot-jinju-sb-1", c.id) for c in scenario.containers]
    plan = solve_plan(scenario, forbidden=forbidden)
    jinju = next(a for a in plan.assignments if a.lot_id == "lot-jinju-sb-1")
    assert jinju.mode == "air"
    assert jinju.reason is not None and jinju.reason.code == "sea_forbidden"


def test_unassigned_keeps_reason_text_and_adds_code(scenario: Scenario) -> None:
    plan = solve_plan(_late(scenario), allow_air=False)
    lost = next(u for u in plan.unassigned if u.lot_id == LATE_LOT)
    assert lost.reason_code == "sea_deadline_miss"
    assert lost.reason == "; ".join(
        [f"{c.id}: 출하일이 선적 마감 이후" for c in scenario.containers] + ["항공: 항공 미허용"]
    )


def test_unassigned_with_options_is_capacity_or_cost(scenario: Scenario) -> None:
    one = [c.model_copy(update={"capacity_pallets": 4}) for c in scenario.containers[:1]]
    small = _with_lot(scenario, "lot-jinju-sb-1", pallets=4)
    plan = solve_plan(small.model_copy(update={"containers": one}), allow_air=False)
    codes = {u.lot_id: u.reason_code for u in plan.unassigned}
    assert codes["lot-sancheong-sb-1"] == "over_capacity"
    fits = [u for u in plan.unassigned if u.pallets <= 4 and u.lot_id != LATE_LOT]
    assert len(fits) == 1
    assert fits[0].reason_code == "capacity_or_cost"


def test_pickup_cutoff_is_tagged(scenario: Scenario) -> None:
    voyages = [
        v.model_copy(update={"cy_cutoff": datetime.fromisoformat("2026-11-17T09:00+09:00")})
        if v.id == "V-2611A"
        else v
        for v in scenario.voyages
    ]
    tight = _with_lot(scenario, "lot-sancheong-sb-1", ready_date=date(2026, 11, 17))
    tight = tight.model_copy(update={"voyages": voyages})
    air = build_plan(tight).plan
    moved = next(a for a in air.assignments if a.lot_id == "lot-sancheong-sb-1")
    assert moved.mode == "air"
    assert moved.reason is not None and moved.reason.code == "pickup_cutoff_miss"
    assert CUTOFF_REASON in moved.reason.message
    lost = next(
        u for u in build_plan(tight, allow_air=False).plan.unassigned if u.lot_id == moved.lot_id
    )
    assert lost.reason_code == "pickup_cutoff_miss"
    assert CUTOFF_REASON in lost.reason


def test_demo_plan_response_carries_reason_fields(client: TestClient) -> None:
    response = client.post("/api/plans", json={"scenario_id": "default"})
    assert response.status_code in (200, 201), response.text
    plan = response.json()["plan"]
    assert all("reason" in a for a in plan["assignments"])
    late = next(a for a in plan["assignments"] if a["lot_id"] == LATE_LOT)
    assert late["reason"]["code"] == "capacity_or_cost"


def test_shipper_view_anonymizes_other_shipper_reasons(
    scenario: Scenario, platform_client: TestClient, saas_login
) -> None:
    ids = []
    for lot in scenario.lots:
        if lot.shipper_id not in ("sh-jinju", "sh-miryang"):
            continue
        body = _lot_body(lot)
        if lot.id == LATE_LOT:
            body["ready_date"] = "2026-11-21"
        client = saas_login(shipper_org_id(lot.shipper_id))
        response = client.post("/api/saas/shipper/lots", json=body)
        assert response.status_code == 201, response.text
        ids.append(response.json()["registered"]["id"])
    created = platform_client.post("/api/saas/platform/plans", json={"lot_ids": ids})
    assert created.status_code == 201, created.text
    plan_id = created.json()["plan_id"]
    platform_plan = created.json()["plan"]
    late = next(a for a in platform_plan["assignments"] if a["lot_id"] == LATE_LOT)
    assert late["reason"]["code"] == "sea_deadline_miss"

    jinju = saas_login(shipper_org_id("sh-jinju"))
    response = jinju.get(f"/api/saas/shipper/plans/{plan_id}")
    assert response.status_code == 200, response.text
    plan = response.json()["plan"]
    others = [a for a in plan["assignments"] if a["shipper_id"] != "sh-jinju"]
    assert others and all(a["shipper_id"].startswith("anon-") for a in others)
    air = [a for a in others if a["mode"] == "air"]
    assert air and all(a["reason"]["code"] == "sea_deadline_miss" for a in air)
    text = str([a["reason"] for a in plan["assignments"]]) + str(plan["unassigned"])
    assert "miryang" not in text and "밀양" not in text
    for row in plan["unassigned"]:
        assert "reason_code" in row
