from typing import Literal

from pydantic import BaseModel


class ParamRange(BaseModel):
    key: str
    label: str
    unit: str
    base: float
    low: float
    high: float
    source: str


class LotChange(BaseModel):
    lot_id: str
    base: str
    case: str


class SensitivityCase(BaseModel):
    value: float
    factor: float
    status: str
    total_savings: int
    savings_delta: int
    total_fee: int
    containers_used: int
    shipper_savings: dict[str, int]
    mode_changed: bool
    changed_lots: list[LotChange]


class ParamSensitivity(BaseModel):
    param: ParamRange
    low: SensitivityCase
    high: SensitivityCase
    conservative: Literal["low", "high"]
    optimistic: Literal["low", "high"]
    savings_impact: int
    fee_impact: int


class TornadoBar(BaseModel):
    key: str
    label: str
    base_savings: int
    savings_low: int
    savings_high: int
    savings_impact: int
    fee_low: int
    fee_high: int


class Threshold(BaseModel):
    key: str
    label: str
    unit: str
    direction: Literal["down", "up"]
    base: float
    threshold: float | None
    factor: float | None
    searched_to: float
    iterations: int
    base_outcome: str
    flipped_outcome: str | None
    changed_lots: list[LotChange]


class AlternativeSummary(BaseModel):
    id: str
    label: str
    feasible: bool
    total_cost: int
    total_savings: int
    total_fee: int
    container_fill: dict[str, int]


class ReplanSensitivity(BaseModel):
    event: dict
    base_outcome: str
    alternatives: list[AlternativeSummary]
    thresholds: list[Threshold]


class SensitivityResult(BaseModel):
    scenario_id: str
    plan_id: str | None
    base_total_savings: int
    base_total_fee: int
    base_shipper_savings: dict[str, int]
    base_routes: dict[str, str]
    params: list[ParamSensitivity]
    tornado: list[TornadoBar]
    thresholds: list[Threshold]
    replan: ReplanSensitivity | None
    solve_count: int
    solve_ms: int


class MethodResult(BaseModel):
    method: str
    label: str
    description: str
    containers_used: int
    avg_fill: float
    sea_pallets: int
    air_pallets: int
    unassigned_lots: int
    unassigned_pallets: int
    total_baseline_cost: int
    total_shipper_cost: int
    total_savings: int
    total_fee: int
    shippers_with_loss: int
    constraint_violations: int
    violation_details: list[str]
    solve_ms: int
    locations: dict[str, str]


class ReplanBenchmarkRow(BaseModel):
    id: str
    label: str
    role: Literal["manual", "recommended", "other"]
    feasible: bool
    total_cost: int
    cost_vs_manual: int
    total_savings: int
    total_fee: int
    container_fill: dict[str, int]
    summary: str


class ReplanBenchmark(BaseModel):
    event: dict
    recommended_id: str | None
    rows: list[ReplanBenchmarkRow]
    improvement_vs_manual: int


class BenchmarkResult(BaseModel):
    scenario_id: str
    methods: list[MethodResult]
    replan: ReplanBenchmark | None
    solve_ms: int


class ContainerEconomics(BaseModel):
    container_id: str
    pallets: int
    capacity_pallets: int
    fill: float
    container_cost: int
    savings: int
    margin_per_pallet: int
    breakeven_savings_pallets: float | None
    breakeven_platform_pallets: float | None
    breakeven_platform_fill: float | None


class VoyageEconomics(BaseModel):
    voyage_id: str
    vessel_name: str
    etd: str
    containers_used: int
    pallets: int
    capacity_pallets: int
    fill: float
    savings: int
    fee_revenue: int
    forwarder_share: int
    ops_cost: int
    platform_net: int
    usage_fee_rule_revenue: int
    breakeven_containers: float | None
    containers: list[ContainerEconomics]


class SeasonLine(BaseModel):
    product_id: str
    tonnes: float
    pallets: int
    containers: float
    savings_per_pallet: int
    revenue_rule: str
    revenue: int
    source: str


class SeasonProjection(BaseModel):
    lines: list[SeasonLine]
    revenue: int
    forwarder_share: int
    fixed_cost: int
    platform_net: int
    breakeven_pallets: int | None
    source: str


class Assumption(BaseModel):
    key: str
    value: float
    unit: str
    source: str


class BusinessResult(BaseModel):
    scenario_id: str
    assumptions: list[Assumption]
    voyages: list[VoyageEconomics]
    total_fee_revenue: int
    total_platform_net: int
    season: SeasonProjection | None
