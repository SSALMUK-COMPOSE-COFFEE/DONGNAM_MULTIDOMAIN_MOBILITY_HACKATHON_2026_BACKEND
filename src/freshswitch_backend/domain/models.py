from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field


class Sourced(BaseModel):
    source: str = "가정"


class Product(Sourced):
    id: str
    name: str
    temp_min_c: float
    temp_max_c: float
    o2_pct: float | None = None
    co2_pct: float | None = None
    shelf_life_days: int
    so2_treated: bool = False
    pallet_kg: float = 415


class Packhouse(Sourced):
    id: str
    name: str
    region: str
    lat: float
    lon: float


class Port(Sourced):
    id: str
    name: str
    unlocode: str
    lat: float
    lon: float


class Shipper(Sourced):
    id: str
    name: str
    packhouse_id: str


class Lot(Sourced):
    id: str
    shipper_id: str
    product_id: str
    pallets: int = Field(gt=0)
    ready_date: date
    destination: str
    sell_by_date: date


class Voyage(Sourced):
    id: str
    vessel_name: str
    terminal: str
    origin: str
    destination: str
    etd: datetime
    cy_cutoff: datetime
    transit_days: float


class Container(Sourced):
    id: str
    voyage_id: str
    kind: str = "40RH-CA"
    capacity_pallets: int = 20


class Truck(Sourced):
    id: str
    depot_id: str
    capacity_pallets: int


class Quantity(BaseModel):
    value: float
    unit: str
    source: str = "가정"


def _q(value: float, unit: str, source: str = "가정") -> Quantity:
    return Quantity(value=value, unit=unit, source=source)


class ProductCostParams(BaseModel):
    value_per_kg_krw: Quantity
    sea_base_loss_rate: Quantity
    loss_rate_per_wait_day: Quantity
    max_sea_loss_rate: Quantity
    early_harvest_loss_rate_per_day: Quantity
    air_loss_rate: Quantity


def _default_product_cost() -> ProductCostParams:
    return ProductCostParams(
        value_per_kg_krw=_q(18000, "원/kg"),
        sea_base_loss_rate=_q(0.015, "비율"),
        loss_rate_per_wait_day=_q(0.009, "비율/일"),
        max_sea_loss_rate=_q(0.08, "비율"),
        early_harvest_loss_rate_per_day=_q(0.02, "비율/일"),
        air_loss_rate=_q(0.01, "비율"),
    )


class CostParams(BaseModel):
    sea_freight_per_container_krw: Quantity = _q(4_560_000, "원/컨테이너")
    sea_ancillary_per_container_krw: Quantity = _q(4_500_000, "원/컨테이너")
    air_freight_per_kg_krw: Quantity = _q(2151, "원/kg")
    reefer_truck_cost_per_km_krw: Quantity = _q(2000, "원/km")
    truck_capacity_pallets: Quantity = _q(16, "파렛트")
    road_distance_factor: Quantity = _q(1.3, "배")
    air_trucking_km: Quantity = _q(350, "km")
    storage_per_pallet_day_krw: Quantity = _q(3000, "원/파렛트·일")
    air_storage_days: Quantity = _q(1.5, "일")
    air_transit_days: Quantity = _q(0.5, "일")
    unassigned_penalty_per_pallet_krw: Quantity = _q(10_000_000, "원/파렛트")
    platform_fee_rate: Quantity = _q(0.25, "비율")
    product_costs: dict[str, ProductCostParams] = Field(default_factory=dict)
    default_product_cost: ProductCostParams = Field(default_factory=_default_product_cost)

    def for_product(self, product_id: str) -> ProductCostParams:
        return self.product_costs.get(product_id, self.default_product_cost)


class Scenario(BaseModel):
    id: str
    name: str
    description: str = ""
    products: list[Product]
    packhouses: list[Packhouse]
    ports: list[Port]
    shippers: list[Shipper]
    lots: list[Lot]
    voyages: list[Voyage]
    containers: list[Container]
    trucks: list[Truck]
    cost_params: CostParams = Field(default_factory=CostParams)


class ScenarioSummary(BaseModel):
    id: str
    name: str
    description: str


Mode = Literal["sea", "air"]


class Assignment(BaseModel):
    lot_id: str
    shipper_id: str
    product_id: str
    pallets: int
    mode: Mode
    container_id: str | None = None
    voyage_id: str | None = None
    pull_forward_days: int = 0


class UnassignedLot(BaseModel):
    lot_id: str
    shipper_id: str
    pallets: int
    reason: str


class ContainerLoad(BaseModel):
    container_id: str
    voyage_id: str
    used: bool
    pallets: int
    capacity_pallets: int
    utilization: float
    lot_ids: list[str]
    product_ids: list[str]
    shipper_ids: list[str]
    container_cost: int


class CostBreakdown(BaseModel):
    freight: int = 0
    handling: int = 0
    collection: int = 0
    storage: int = 0
    loss_risk: int = 0
    total: int = 0


class ShipperCost(BaseModel):
    shipper_id: str
    pallets: int
    sea_pallets: int
    air_pallets: int
    plan: CostBreakdown
    baseline: CostBreakdown


class Settlement(BaseModel):
    shipper_id: str
    baseline_cost: int
    shared_cost: int
    savings: int
    fee: int
    shipper_gain: int


class Plan(BaseModel):
    scenario_id: str
    status: str
    objective: int
    solve_ms: int
    assignments: list[Assignment]
    unassigned: list[UnassignedLot]
    containers: list[ContainerLoad]
    shipper_costs: list[ShipperCost]
    settlements: list[Settlement]
    total_baseline_cost: int
    total_shared_cost: int
    total_savings: int
    total_fee: int
