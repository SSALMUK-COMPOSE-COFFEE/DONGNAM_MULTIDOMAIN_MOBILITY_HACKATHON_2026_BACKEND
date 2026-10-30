from freshswitch_backend.domain.models import LossAdjustment, Plan, Scenario
from freshswitch_backend.optimize.costs import CostModel
from freshswitch_backend.planning.service import build_plan
from freshswitch_backend.quality.models import ImpactResult, LossCell, LotShift, ShipperImpact


def _placement(plan: Plan, lot_id: str) -> str:
    assignment = next((a for a in plan.assignments if a.lot_id == lot_id), None)
    if assignment is None:
        return "미배정"
    if assignment.mode == "air":
        return "항공"
    return f"해상 {assignment.voyage_id}"


def _won(value: int) -> str:
    return f"{value:,}원"


def learning_impact(
    scenario: Scenario, adjustments: list[LossAdjustment], cells: list[LossCell]
) -> ImpactResult:
    prior_scenario = scenario.model_copy(
        update={"cost_params": scenario.cost_params.model_copy(update={"loss_adjustments": []})}
    )
    learned_scenario = scenario.model_copy(
        update={
            "cost_params": scenario.cost_params.model_copy(update={"loss_adjustments": adjustments})
        }
    )
    prior = build_plan(prior_scenario.model_copy(deep=True)).plan
    learned = build_plan(learned_scenario.model_copy(deep=True)).plan
    names = {s.id: s.name for s in scenario.shippers}
    model = CostModel(scenario)
    lots = {lot.id: lot for lot in scenario.lots}
    sea_value: dict[str, float] = {}
    for a in learned.assignments:
        if a.mode == "sea" and a.lot_id in lots:
            sea_value[a.shipper_id] = sea_value.get(a.shipper_id, 0.0) + model.lot_value(
                lots[a.lot_id]
            )
    prior_settle = {s.shipper_id: s for s in prior.settlements}
    learned_settle = {s.shipper_id: s for s in learned.settlements}
    prior_cost = {c.shipper_id: c for c in prior.shipper_costs}
    learned_cost = {c.shipper_id: c for c in learned.shipper_costs}
    shippers = []
    for shipper in scenario.shippers:
        a, b = prior_settle.get(shipper.id), learned_settle.get(shipper.id)
        ca, cb = prior_cost.get(shipper.id), learned_cost.get(shipper.id)
        if a is None and b is None:
            continue
        shippers.append(
            ShipperImpact(
                shipper_id=shipper.id,
                shipper_name=names[shipper.id],
                savings_prior=a.savings if a else 0,
                savings_learned=b.savings if b else 0,
                savings_change=(b.savings if b else 0) - (a.savings if a else 0),
                fee_prior=a.fee if a else 0,
                fee_learned=b.fee if b else 0,
                loss_risk_prior=ca.plan.loss_risk if ca else 0,
                loss_risk_learned=cb.plan.loss_risk if cb else 0,
                sea_pallets_prior=ca.sea_pallets if ca else 0,
                sea_pallets_learned=cb.sea_pallets if cb else 0,
                air_pallets_prior=ca.air_pallets if ca else 0,
                air_pallets_learned=cb.air_pallets if cb else 0,
                sea_cargo_value=round(sea_value.get(shipper.id, 0.0)),
                air_switch_margin_rate=round((b.savings if b else 0) / sea_value[shipper.id], 6)
                if sea_value.get(shipper.id)
                else None,
            )
        )
    shifts = []
    for lot in scenario.lots:
        before, after = _placement(prior, lot.id), _placement(learned, lot.id)
        if before != after:
            shifts.append(
                LotShift(lot_id=lot.id, shipper_id=lot.shipper_id, before=before, after=after)
            )
    change = learned.total_savings - prior.total_savings
    moved = (
        ", ".join(f"{s.lot_id} {s.before}→{s.after}" for s in shifts)
        if shifts
        else "배정 변화 없음"
    )
    summary = (
        f"학습 손실률 {len(adjustments)}칸 반영: 총 절감 {_won(prior.total_savings)} → "
        f"{_won(learned.total_savings)} ({change:+,}원), 성과보수 {_won(prior.total_fee)} → "
        f"{_won(learned.total_fee)}. {moved}"
    )
    return ImpactResult(
        scenario_id=scenario.id,
        adjustments=adjustments,
        cells=cells,
        shippers=shippers,
        lot_shifts=shifts,
        total_savings_prior=prior.total_savings,
        total_savings_learned=learned.total_savings,
        total_savings_change=change,
        total_fee_prior=prior.total_fee,
        total_fee_learned=learned.total_fee,
        summary=summary,
    )
