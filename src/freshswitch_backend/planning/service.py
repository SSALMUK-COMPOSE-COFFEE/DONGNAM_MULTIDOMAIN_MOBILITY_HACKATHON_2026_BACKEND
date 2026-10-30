from collections import defaultdict
from datetime import timedelta

from pydantic import BaseModel

from freshswitch_backend.domain.models import Plan, Scenario
from freshswitch_backend.optimize import solve_plan
from freshswitch_backend.routing import PickupPlan, build_pickup_request, solve_pickup

ROUTE_TIME_LIMIT_S = 1.0


class VoyagePickup(BaseModel):
    voyage_id: str
    lot_ids: list[str]
    pickup: PickupPlan


class PlanWithRoutes(BaseModel):
    plan: Plan
    routes: list[VoyagePickup]


def build_routes(
    scenario: Scenario, plan: Plan, time_limit_s: float = ROUTE_TIME_LIMIT_S
) -> list[VoyagePickup]:
    pulls = {a.lot_id: a.pull_forward_days for a in plan.assignments if a.pull_forward_days}
    effective = scenario.model_copy(
        update={
            "lots": [
                lot.model_copy(
                    update={"ready_date": lot.ready_date - timedelta(days=pulls[lot.id])}
                )
                if lot.id in pulls
                else lot
                for lot in scenario.lots
            ]
        }
    )
    by_voyage: dict[str, list[str]] = defaultdict(list)
    for a in plan.assignments:
        if a.mode == "sea" and a.voyage_id:
            by_voyage[a.voyage_id].append(a.lot_id)
    order = {v.id: i for i, v in enumerate(scenario.voyages)}
    return [
        VoyagePickup(
            voyage_id=voyage_id,
            lot_ids=lot_ids,
            pickup=solve_pickup(
                build_pickup_request(effective, lot_ids, voyage_id), time_limit_s=time_limit_s
            ),
        )
        for voyage_id, lot_ids in sorted(by_voyage.items(), key=lambda kv: order[kv[0]])
    ]


def with_routes(scenario: Scenario, plan: Plan) -> PlanWithRoutes:
    return PlanWithRoutes(plan=plan, routes=build_routes(scenario, plan))


def build_plan(scenario: Scenario, **options) -> PlanWithRoutes:
    return with_routes(scenario, solve_plan(scenario, **options))
