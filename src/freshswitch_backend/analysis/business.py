from collections import defaultdict

from freshswitch_backend.analysis.common import solve
from freshswitch_backend.analysis.models import (
    Assumption,
    BusinessResult,
    ContainerEconomics,
    SeasonLine,
    SeasonProjection,
    VoyageEconomics,
)
from freshswitch_backend.domain.models import Plan, Scenario

ANNUAL_FIXED_COST = 150_000_000
VOYAGES_PER_YEAR = 52
DEFAULT_FORWARDER_SHARE = 0.4
DEFAULT_OPS_COST_PER_VOYAGE = round(ANNUAL_FIXED_COST / VOYAGES_PER_YEAR)
USAGE_FEE_PER_PALLET = 20_000
USAGE_FEE_PRODUCTS = frozenset({"grape_so2"})
PALLET_KG = 415
PROPOSAL = "기획서 ㅍ3"
SEASON_VOLUMES: tuple[tuple[str, float, str], ...] = (
    ("strawberry", 109.0, f"{PROPOSAL} 8-3: 1년 차 1단계 딸기(홍콩·베트남) 점유 10% = 109t"),
    ("grape_so2", 298.0, f"{PROPOSAL} 8-3: 1년 차 포도 합승 점유 3% = 298t"),
)


class _LotMoney:
    def __init__(self, plan: Plan) -> None:
        self.savings: dict[str, float] = {}
        self.fee: dict[str, float] = {}
        sea_pallets = {c.shipper_id: c.sea_pallets for c in plan.shipper_costs}
        by_shipper = {s.shipper_id: s for s in plan.settlements}
        for a in plan.assignments:
            if a.mode != "sea":
                continue
            settlement = by_shipper.get(a.shipper_id)
            total = sea_pallets.get(a.shipper_id, 0)
            share = a.pallets / total if settlement and total else 0.0
            self.savings[a.lot_id] = settlement.savings * share if settlement else 0.0
            self.fee[a.lot_id] = settlement.fee * share if settlement else 0.0


def _breakeven(numerator: float, margin: float) -> float | None:
    return round(numerator / margin, 2) if margin > 0 else None


def voyage_economics(
    scenario: Scenario,
    plan: Plan,
    forwarder_share: float,
    ops_cost_per_voyage: int,
) -> list[VoyageEconomics]:
    fee_rate = scenario.cost_params.platform_fee_rate.value
    keep = fee_rate * (1 - forwarder_share)
    money = _LotMoney(plan)
    lots_in: dict[str, list] = defaultdict(list)
    for a in plan.assignments:
        if a.mode == "sea" and a.container_id:
            lots_in[a.container_id].append(a)
    result = []
    for voyage in scenario.voyages:
        used = [c for c in plan.containers if c.voyage_id == voyage.id and c.used]
        if not used:
            continue
        ops_per_container = ops_cost_per_voyage / len(used)
        rows = []
        fee_total = usage_rule_total = 0.0
        for load in used:
            items = lots_in[load.container_id]
            fixed = load.container_cost + getattr(load, "terminal_plug_cost", 0)
            savings = sum(money.savings[a.lot_id] for a in items)
            fee = sum(money.fee[a.lot_id] for a in items)
            fee_total += fee
            usage_rule_total += sum(
                USAGE_FEE_PER_PALLET * a.pallets
                if a.product_id in USAGE_FEE_PRODUCTS
                else money.fee[a.lot_id]
                for a in items
            )
            margin = (savings + fixed) / load.pallets if load.pallets else 0.0
            platform = _breakeven(ops_per_container / keep + fixed, margin) if keep > 0 else None
            rows.append(
                ContainerEconomics(
                    container_id=load.container_id,
                    pallets=load.pallets,
                    capacity_pallets=load.capacity_pallets,
                    fill=load.utilization,
                    container_cost=fixed,
                    savings=round(savings),
                    margin_per_pallet=round(margin),
                    breakeven_savings_pallets=_breakeven(fixed, margin),
                    breakeven_platform_pallets=platform,
                    breakeven_platform_fill=round(platform / load.capacity_pallets, 4)
                    if platform is not None
                    else None,
                )
            )
        pallets = sum(c.pallets for c in used)
        capacity = sum(c.capacity_pallets for c in used)
        fee_revenue = round(fee_total)
        forwarder = round(fee_total * forwarder_share)
        result.append(
            VoyageEconomics(
                voyage_id=voyage.id,
                vessel_name=voyage.vessel_name,
                etd=voyage.etd.isoformat(),
                containers_used=len(used),
                pallets=pallets,
                capacity_pallets=capacity,
                fill=round(pallets / capacity, 4),
                savings=sum(r.savings for r in rows),
                fee_revenue=fee_revenue,
                forwarder_share=forwarder,
                ops_cost=ops_cost_per_voyage,
                platform_net=fee_revenue - forwarder - ops_cost_per_voyage,
                usage_fee_rule_revenue=round(usage_rule_total),
                breakeven_containers=_breakeven(
                    ops_cost_per_voyage, fee_total * (1 - forwarder_share) / len(used)
                ),
                containers=rows,
            )
        )
    return result


def season_projection(
    scenario: Scenario, plan: Plan, forwarder_share: float
) -> SeasonProjection | None:
    product_ids = {p.id for p in scenario.products}
    if not all(pid in product_ids for pid, _, _ in SEASON_VOLUMES):
        return None
    money = _LotMoney(plan)
    fee_rate = scenario.cost_params.platform_fee_rate.value
    lines = []
    for product_id, tonnes, source in SEASON_VOLUMES:
        sea = [a for a in plan.assignments if a.mode == "sea" and a.product_id == product_id]
        sea_pallets = sum(a.pallets for a in sea)
        if not sea_pallets:
            return None
        per_pallet = sum(money.savings[a.lot_id] for a in sea) / sea_pallets
        containers = {a.container_id for a in sea}
        per_container = sea_pallets / len(containers)
        pallets = round(tonnes * 1000 / PALLET_KG)
        if product_id in USAGE_FEE_PRODUCTS:
            rule = f"파렛트 이용료 {USAGE_FEE_PER_PALLET:,}원({PROPOSAL} 8-1: 이미 배로 가는 품목)"
            revenue = pallets * USAGE_FEE_PER_PALLET
        else:
            rule = f"절감액 × {fee_rate:.0%}({PROPOSAL} 8-1)"
            revenue = round(pallets * per_pallet * fee_rate)
        lines.append(
            SeasonLine(
                product_id=product_id,
                tonnes=tonnes,
                pallets=pallets,
                containers=round(pallets / per_container, 1),
                savings_per_pallet=round(per_pallet),
                revenue_rule=rule,
                revenue=revenue,
                source=source,
            )
        )
    revenue = sum(line.revenue for line in lines)
    forwarder = round(revenue * forwarder_share)
    net_per_pallet = (revenue - forwarder) / sum(line.pallets for line in lines)
    return SeasonProjection(
        lines=lines,
        revenue=revenue,
        forwarder_share=forwarder,
        fixed_cost=ANNUAL_FIXED_COST,
        platform_net=revenue - forwarder - ANNUAL_FIXED_COST,
        breakeven_pallets=round(ANNUAL_FIXED_COST / net_per_pallet) if net_per_pallet > 0 else None,
        source=f"{PROPOSAL} 8-3 물량(점유율은 기획서 가정), 8-1 고정비 연 1.5억 원(가정). "
        "파렛트당 절감액은 이 시나리오 최적화 결과 평균",
    )


def business(
    scenario: Scenario,
    forwarder_share: float = DEFAULT_FORWARDER_SHARE,
    ops_cost_per_voyage: int = DEFAULT_OPS_COST_PER_VOYAGE,
) -> BusinessResult:
    plan = solve(scenario)
    voyages = voyage_economics(scenario, plan, forwarder_share, ops_cost_per_voyage)
    fee_rate = scenario.cost_params.platform_fee_rate
    assumptions = [
        Assumption(
            key="platform_fee_rate", value=fee_rate.value, unit="비율", source=fee_rate.source
        ),
        Assumption(
            key="forwarder_share",
            value=forwarder_share,
            unit="수수료 대비 비율",
            source="가정: 문서에 포워더 배분 수치 없음. 1년 차는 포워더 명의 운송(기획서 8-1)",
        ),
        Assumption(
            key="ops_cost_per_voyage",
            value=ops_cost_per_voyage,
            unit="원/항차",
            source=f"가정: {PROPOSAL} 8-1 고정비 연 1.5억 원 ÷ 연 {VOYAGES_PER_YEAR}항차(주 1회)",
        ),
        Assumption(
            key="usage_fee_per_pallet",
            value=USAGE_FEE_PER_PALLET,
            unit="원/파렛트",
            source=f"{PROPOSAL} 8-1: 포도는 성과보수 없이 파렛트 이용료 2만 원",
        ),
    ]
    return BusinessResult(
        scenario_id=scenario.id,
        assumptions=assumptions,
        voyages=voyages,
        total_fee_revenue=sum(v.fee_revenue for v in voyages),
        total_platform_net=sum(v.platform_net for v in voyages),
        season=season_projection(scenario, plan, forwarder_share),
    )
