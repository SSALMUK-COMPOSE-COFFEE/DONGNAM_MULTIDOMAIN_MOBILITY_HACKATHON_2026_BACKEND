from collections import defaultdict
from collections.abc import Mapping

from freshswitch_backend.domain.models import (
    Assignment,
    ContainerLoad,
    CostBreakdown,
    Lot,
    Settlement,
    ShipperCost,
)
from freshswitch_backend.optimize.costs import CostModel, LotCost


def settle(savings: int, fee_rate: float) -> tuple[int, int]:
    if savings <= 0:
        return 0, savings
    fee = round(savings * fee_rate)
    return fee, savings - fee


def _add(acc: dict[str, float], cost: LotCost) -> None:
    acc["freight"] += cost.freight
    acc["handling"] += cost.handling
    acc["collection"] += cost.collection
    acc["storage"] += cost.storage
    acc["loss_risk"] += cost.loss_risk


PARTS = ("freight", "handling", "collection", "storage", "loss_risk")


def _breakdown(
    acc: dict[str, float], estimate: float | None = None, routed: bool = False
) -> CostBreakdown:
    parts = {k: round(acc[k]) for k in PARTS}
    return CostBreakdown(
        **parts,
        total=sum(parts.values()),
        collection_estimate=round(estimate) if estimate is not None else None,
        collection_route=parts["collection"] if routed else None,
    )


def container_loads(model: CostModel, assignments: list[Assignment]) -> list[ContainerLoad]:
    by_container: dict[str, list[Assignment]] = defaultdict(list)
    for a in assignments:
        if a.mode == "sea" and a.container_id:
            by_container[a.container_id].append(a)
    loads = []
    for container in model.scenario.containers:
        items = by_container.get(container.id, [])
        pallets = sum(a.pallets for a in items)
        extra_days = model.terminal_extra_days(model.voyage_of(container))
        loads.append(
            ContainerLoad(
                container_id=container.id,
                voyage_id=container.voyage_id,
                used=bool(items),
                pallets=pallets,
                capacity_pallets=container.capacity_pallets,
                utilization=round(pallets / container.capacity_pallets, 4),
                lot_ids=[a.lot_id for a in items],
                product_ids=sorted({a.product_id for a in items}),
                shipper_ids=sorted({a.shipper_id for a in items}),
                container_cost=round(model.container_fixed_cost) if items else 0,
                terminal_extra_days=round(extra_days, 2),
                terminal_plug_cost=round(model.container_plug_cost(container)) if items else 0,
            )
        )
    return loads


def lot_plan_cost(
    model: CostModel,
    lot: Lot,
    assignment: Assignment,
    container_pallets: dict[str, int],
    collection: float | None = None,
) -> LotCost:
    if assignment.mode == "air":
        return model.air_lot_cost(lot)
    container = model.containers[assignment.container_id]
    voyage = model.voyage_of(container)
    variable = model.sea_lot_cost(lot, voyage, assignment.pull_forward_days)
    share = lot.pallets / container_pallets[container.id]
    return LotCost(
        freight=model.container_freight * share,
        handling=model.container_handling * share,
        collection=variable.collection if collection is None else collection,
        storage=variable.storage + model.container_plug_cost(container) * share,
        loss_risk=variable.loss_risk,
    )


def shipper_costs_and_settlements(
    model: CostModel,
    lots: dict[str, Lot],
    assignments: list[Assignment],
    route_collection: Mapping[str, int] | None = None,
) -> tuple[list[ShipperCost], list[Settlement]]:
    container_pallets: dict[str, int] = defaultdict(int)
    for a in assignments:
        if a.mode == "sea" and a.container_id:
            container_pallets[a.container_id] += a.pallets
    plan_acc: dict[str, dict[str, float]] = {}
    base_acc: dict[str, dict[str, float]] = {}
    estimates: dict[str, float] = defaultdict(float)
    pallets: dict[str, list[int]] = {}
    routed = route_collection is not None
    for a in assignments:
        lot = lots[a.lot_id]
        sid = lot.shipper_id
        plan_acc.setdefault(sid, defaultdict(float))
        base_acc.setdefault(sid, defaultdict(float))
        pallets.setdefault(sid, [0, 0])
        estimated = lot_plan_cost(model, lot, a, container_pallets)
        estimates[sid] += estimated.collection
        override = (route_collection or {}).get(a.lot_id) if a.mode == "sea" else None
        _add(plan_acc[sid], lot_plan_cost(model, lot, a, container_pallets, override))
        _add(base_acc[sid], model.air_lot_cost(lot))
        pallets[sid][0 if a.mode == "sea" else 1] += lot.pallets
    fee_rate = model.params.platform_fee_rate.value
    costs: list[ShipperCost] = []
    settlements: list[Settlement] = []
    for shipper in model.scenario.shippers:
        sid = shipper.id
        if sid not in plan_acc:
            continue
        plan = _breakdown(plan_acc[sid], estimates[sid], routed)
        baseline = _breakdown(base_acc[sid])
        sea, air = pallets[sid]
        costs.append(
            ShipperCost(
                shipper_id=sid,
                pallets=sea + air,
                sea_pallets=sea,
                air_pallets=air,
                plan=plan,
                baseline=baseline,
            )
        )
        savings = baseline.total - plan.total
        fee, gain = settle(savings, fee_rate)
        settlements.append(
            Settlement(
                shipper_id=sid,
                baseline_cost=baseline.total,
                shared_cost=plan.total,
                savings=savings,
                fee=fee,
                shipper_gain=gain,
            )
        )
    return costs, settlements
