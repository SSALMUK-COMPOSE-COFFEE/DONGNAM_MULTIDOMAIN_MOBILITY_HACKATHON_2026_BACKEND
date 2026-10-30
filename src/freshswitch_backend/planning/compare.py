from typing import Literal

from pydantic import BaseModel

from freshswitch_backend.domain.models import Assignment, CostBreakdown, Plan, Scenario
from freshswitch_backend.optimize.costs import CostModel, LotCost
from freshswitch_backend.optimize.feasibility import air_rejection, products_compatible, sea_option
from freshswitch_backend.optimize.settlement import lot_plan_cost

MAX_PULL_FORWARD_DAYS = 2


class SeaQuote(BaseModel):
    feasible: bool
    in_plan: bool
    container_id: str | None = None
    voyage_id: str | None = None
    pull_forward_days: int = 0
    container_pallets_after: int | None = None
    cost: CostBreakdown | None = None
    reason: str | None = None


class AirQuote(BaseModel):
    feasible: bool
    cost: CostBreakdown
    reason: str | None = None


class AirSeaComparison(BaseModel):
    scenario_id: str
    lot_id: str
    shipper_id: str
    product_id: str
    pallets: int
    kg: float
    plan_mode: Literal["sea", "air", "unassigned"]
    sea: SeaQuote
    air: AirQuote
    cheaper: Literal["sea", "air"] | None
    sea_savings: int | None


def breakdown(cost: LotCost) -> CostBreakdown:
    parts = {
        "freight": round(cost.freight),
        "handling": round(cost.handling),
        "collection": round(cost.collection),
        "storage": round(cost.storage),
        "loss_risk": round(cost.loss_risk),
    }
    return CostBreakdown(**parts, total=sum(parts.values()))


def _container_pallets(plan: Plan) -> dict[str, int]:
    return {c.container_id: c.pallets for c in plan.containers}


def _sea_quote(model: CostModel, plan: Plan, lot_id: str) -> SeaQuote:
    lots = {lot.id: lot for lot in model.scenario.lots}
    lot = lots[lot_id]
    loads = _container_pallets(plan)
    assigned = next((a for a in plan.assignments if a.lot_id == lot_id), None)
    if assigned is not None and assigned.mode == "sea":
        cost = lot_plan_cost(model, lot, assigned, loads)
        return SeaQuote(
            feasible=True,
            in_plan=True,
            container_id=assigned.container_id,
            voyage_id=assigned.voyage_id,
            pull_forward_days=assigned.pull_forward_days,
            container_pallets_after=loads[assigned.container_id],
            cost=breakdown(cost),
        )
    products = {c.container_id: c.product_ids for c in plan.containers}
    best: SeaQuote | None = None
    reasons: list[str] = []
    for container in model.scenario.containers:
        option, reason = sea_option(model, lot, container, MAX_PULL_FORWARD_DAYS)
        if option is None:
            reasons.append(f"{container.id}: {reason}")
            continue
        load = loads.get(container.id, 0) + lot.pallets
        if load > container.capacity_pallets:
            reasons.append(f"{container.id}: 남은 자리 부족")
            continue
        clash = [
            p
            for p in products.get(container.id, [])
            if not products_compatible(model.products[p], model.product(lot))
        ]
        if clash:
            reasons.append(f"{container.id}: 혼적 불가({', '.join(clash)})")
            continue
        hypothetical = Assignment(
            lot_id=lot.id,
            shipper_id=lot.shipper_id,
            product_id=lot.product_id,
            pallets=lot.pallets,
            mode="sea",
            container_id=container.id,
            voyage_id=container.voyage_id,
            pull_forward_days=option.pull_forward_days,
        )
        cost = breakdown(lot_plan_cost(model, lot, hypothetical, {container.id: load}))
        if best is None or cost.total < best.cost.total:
            best = SeaQuote(
                feasible=True,
                in_plan=False,
                container_id=container.id,
                voyage_id=container.voyage_id,
                pull_forward_days=option.pull_forward_days,
                container_pallets_after=load,
                cost=cost,
            )
    if best is not None:
        return best
    return SeaQuote(feasible=False, in_plan=False, reason="; ".join(reasons) or "해상 후보 없음")


def compare_air_sea(scenario: Scenario, plan: Plan, lot_id: str) -> AirSeaComparison:
    model = CostModel(scenario)
    lot = next((x for x in scenario.lots if x.id == lot_id), None)
    if lot is None:
        raise KeyError(lot_id)
    assigned = next((a for a in plan.assignments if a.lot_id == lot_id), None)
    sea = _sea_quote(model, plan, lot_id)
    air_reason = air_rejection(model, lot)
    air = AirQuote(
        feasible=air_reason is None, cost=breakdown(model.air_lot_cost(lot)), reason=air_reason
    )
    options = []
    if sea.feasible and sea.cost is not None:
        options.append(("sea", sea.cost.total))
    if air.feasible:
        options.append(("air", air.cost.total))
    cheaper = min(options, key=lambda o: o[1])[0] if options else None
    return AirSeaComparison(
        scenario_id=scenario.id,
        lot_id=lot.id,
        shipper_id=lot.shipper_id,
        product_id=lot.product_id,
        pallets=lot.pallets,
        kg=model.lot_kg(lot),
        plan_mode=assigned.mode if assigned else "unassigned",
        sea=sea,
        air=air,
        cheaper=cheaper,
        sea_savings=air.cost.total - sea.cost.total if sea.cost is not None else None,
    )
