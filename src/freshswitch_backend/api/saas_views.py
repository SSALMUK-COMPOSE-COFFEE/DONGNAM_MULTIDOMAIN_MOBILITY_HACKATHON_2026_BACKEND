import re
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import select

from freshswitch_backend.domain.models import (
    Scenario,
    SettlementBaselineKind,
    SettlementBillingRule,
)
from freshswitch_backend.execution.models import ExecutionOrder, ShipperConfirmation
from freshswitch_backend.execution.orders import short_name
from freshswitch_backend.execution.quality import QualitySettlement, adjusted_settlements
from freshswitch_backend.persistence import database
from freshswitch_backend.persistence.tables import LotRow, OrganizationRow, PlanForwarderRow
from freshswitch_backend.planning.store import PlanSession
from freshswitch_backend.replan.models import (
    AddLotEvent,
    CancellationCharge,
    CancellationTransfer,
    FixedLot,
    RouteChange,
)

MONEY_TOTALS = (
    "total_baseline_cost",
    "total_shared_cost",
    "total_savings",
    "total_fee",
    "total_operational_baseline_cost",
    "total_operational_cost",
    "total_operational_savings",
)
MONEY_LISTS = ("settlements", "container_settlements", "settlement_tariffs", "shipper_costs")
HISTORY_MONEY = ("cost_before", "cost_after", "cost_delta")
CONFIRMATION_MONEY = (
    "cost_before",
    "cost_after",
    "cost_delta",
    "cancellation_adjustment",
    "net_cost_delta",
)
CANCELLATION_MONEY = (
    "container_cost",
    "share_per_pallet_before",
    "share_per_pallet_after",
    "fee",
    "consolidator_amount",
)
INSPECTION_FIELDS = (
    "lot_id",
    "shipper_id",
    "planned_loss_rate",
    "inspected_loss_rate",
    "inspected_at",
)
SETTLEMENT_ACTION = "settlement_recompute"
SETTLEMENT_TITLE = "정산 재계산"
ROUTE_FIELDS = frozenset(
    {
        "truck_id",
        "trip",
        "cargo_group",
        "capacity_pallets",
        "load_pallets",
        "stops",
        "earliest_departure",
        "latest_departure",
        "port_eta",
        "slack_minutes",
        "gate_status",
        "gate_window",
        "gate_delay_minutes",
        "gate_slot_start",
        "gate_slot_end",
    }
)
STOP_FIELDS = frozenset(
    {
        "stop_id",
        "packhouse_id",
        "lot_ids",
        "sequence",
        "pallets",
        "load_after",
        "ready_at",
        "eta",
        "latest_arrival",
        "cargo_group",
    }
)
PICKUP_FIELDS = frozenset(
    {
        "status",
        "deadline",
        "routes",
        "dropped_stop_ids",
        "solve_ms",
        "assumptions",
        "gate_mode",
        "gate_windows",
    }
)
ROUTE_CHANGE_MONEY = ("km_before", "km_after", "cost_before", "cost_after")
ROUTE_CHANGE_PACKHOUSES = (
    "packhouses_before",
    "packhouses_after",
    "packhouses_added",
    "packhouses_removed",
)
ROUTE_CHANGE_LOTS = ("lot_ids_before", "lot_ids_after")
UNKNOWN_OWNER_LABEL = "x"
TOTAL_COST_CLAUSE = re.compile(
    r"이며 총 비용은 (?:변동 없습니다|\S+(?: 원)? (?:늘어납니다|줄어듭니다))\."
)


class ForwarderShipperConfirmation(ShipperConfirmation):
    cost_before: int | None = None
    cost_after: int | None = None
    cost_delta: int | None = None
    cancellation_adjustment: int | None = None
    net_cost_delta: int | None = None


class ForwarderCancellationTransfer(CancellationTransfer):
    share_increase: int | None = None
    amount: int | None = None


class ForwarderCancellation(CancellationCharge):
    container_cost: int | None = None
    share_per_pallet_before: int | None = None
    share_per_pallet_after: int | None = None
    fee: int | None = None
    consolidator_amount: int | None = None
    transfers: list[ForwarderCancellationTransfer]


class ForwarderOrder(ExecutionOrder):
    shipper_confirmations: list[ForwarderShipperConfirmation]
    cancellation: ForwarderCancellation | None = None


class ShipperRouteChange(RouteChange):
    km_before: float | None = None
    km_after: float | None = None
    cost_before: int | None = None
    cost_after: int | None = None


class ForwarderRef(BaseModel):
    org_id: str
    org_name: str


class PlanListItem(BaseModel):
    plan_id: str
    created_at: datetime
    status: str
    vessel: str | None
    departure: datetime | None
    shipper_count: int
    total_pallets: int
    forwarder: ForwarderRef | None = None


class ShipperPlanItem(BaseModel):
    plan_id: str
    created_at: datetime
    status: str
    vessel: str | None
    departure: datetime | None
    my_lot_ids: list[str]
    my_pallets: int
    total_pallets: int


class ShipperSettlementRow(QualitySettlement):
    plan_id: str
    version: int
    vessel: str | None
    departure: datetime | None


class PlatformShipperShare(BaseModel):
    shipper_id: str
    shipper_org_name: str | None
    saving_krw: int
    fee_krw: int
    billing_rule: SettlementBillingRule = "performance"
    performance_fee_krw: int = Field(
        default=0,
        description="환급 전 금액. fee_before_refund_krw = performance_fee_krw + usage_fee_krw(시범 항차 제외)",
    )
    usage_fee_krw: int = Field(
        default=0,
        description="환급 전 금액. fee_before_refund_krw = performance_fee_krw + usage_fee_krw(시범 항차 제외)",
    )
    fee_before_refund_krw: int = 0
    quality_refund_krw: int = 0
    baseline_kind: SettlementBaselineKind = "air_solo"
    baseline_label: str = ""
    savings_baseline_label: str = ""
    pilot: bool = False


class PlatformSettlement(BaseModel):
    plan_id: str
    vessel: str | None
    departure: datetime | None
    total_saving_krw: int
    platform_fee_krw: int
    shippers: list[PlatformShipperShare]
    performance_fee_krw: int = Field(
        default=0,
        description="환급 전 금액. fee_before_refund_krw = performance_fee_krw + usage_fee_krw(시범 항차 제외)",
    )
    usage_fee_krw: int = Field(
        default=0,
        description="환급 전 금액. fee_before_refund_krw = performance_fee_krw + usage_fee_krw(시범 항차 제외)",
    )
    fee_before_refund_krw: int = 0
    quality_refund_krw: int = 0
    pilot: bool = False


OTHER_SHIPPER_LABEL = "다른 화주"


def _label(index: int) -> str:
    return str(index + 1)


def _first_voyage(session: PlanSession) -> tuple[str | None, datetime | None]:
    used = {c.voyage_id for c in session.plan.containers if c.used}
    voyages = sorted(
        (v for v in session.scenario.voyages if v.id in used), key=lambda v: (v.etd, v.id)
    )
    if not voyages:
        return None, None
    return voyages[0].vessel_name, voyages[0].etd


def total_pallets(session: PlanSession) -> int:
    return sum(a.pallets for a in session.plan.assignments)


def plan_item(session: PlanSession, forwarder: ForwarderRef | None) -> PlanListItem:
    vessel, departure = _first_voyage(session)
    return PlanListItem(
        plan_id=session.plan_id,
        created_at=session.created_at,
        status=session.status,
        vessel=vessel,
        departure=departure,
        shipper_count=len({lot.shipper_id for lot in session.scenario.lots}),
        total_pallets=total_pallets(session),
        forwarder=forwarder,
    )


def shipper_plan_item(session: PlanSession, my_lot_ids: set[str]) -> ShipperPlanItem:
    vessel, departure = _first_voyage(session)
    mine = [lot.id for lot in session.scenario.lots if lot.id in my_lot_ids]
    return ShipperPlanItem(
        plan_id=session.plan_id,
        created_at=session.created_at,
        status=session.status,
        vessel=vessel,
        departure=departure,
        my_lot_ids=mine,
        my_pallets=sum(a.pallets for a in session.plan.assignments if a.lot_id in my_lot_ids),
        total_pallets=total_pallets(session),
    )


def shipper_settlements(
    session: PlanSession, shipper_ids: Iterable[str]
) -> list[ShipperSettlementRow]:
    own = set(shipper_ids)
    vessel, departure = _first_voyage(session)
    return [
        ShipperSettlementRow(
            **row.model_dump(),
            plan_id=session.plan_id,
            version=session.version,
            vessel=vessel,
            departure=departure,
        )
        for row in adjusted_settlements(session.plan, session.arrival_inspections)
        if row.shipper_id in own
    ]


def platform_settlement(session: PlanSession, org_names: dict[str, str]) -> PlatformSettlement:
    vessel, departure = _first_voyage(session)
    rows = adjusted_settlements(session.plan, session.arrival_inspections)
    return PlatformSettlement(
        plan_id=session.plan_id,
        vessel=vessel,
        departure=departure,
        total_saving_krw=sum(r.savings for r in rows),
        platform_fee_krw=sum(r.fee for r in rows),
        performance_fee_krw=sum(r.performance_fee for r in rows),
        usage_fee_krw=sum(r.usage_fee for r in rows),
        fee_before_refund_krw=sum(r.fee_before_refund for r in rows),
        quality_refund_krw=sum(r.quality_refund for r in rows),
        pilot=any(r.pilot for r in rows),
        shippers=[
            PlatformShipperShare(
                shipper_id=r.shipper_id,
                shipper_org_name=org_names.get(r.shipper_id),
                saving_krw=r.savings,
                fee_krw=r.fee,
                billing_rule=r.billing_rule,
                performance_fee_krw=r.performance_fee,
                usage_fee_krw=r.usage_fee,
                fee_before_refund_krw=r.fee_before_refund,
                quality_refund_krw=r.quality_refund,
                baseline_kind=r.baseline_kind,
                baseline_label=r.baseline_label,
                savings_baseline_label=r.savings_baseline_label,
                pilot=r.pilot,
            )
            for r in rows
        ],
    )


def forwarders_of(plan_ids: list[str]) -> dict[str, ForwarderRef]:
    if not plan_ids:
        return {}
    with database.begin() as db:
        rows = db.execute(
            select(PlanForwarderRow.plan_id, OrganizationRow.id, OrganizationRow.name)
            .join(OrganizationRow, OrganizationRow.id == PlanForwarderRow.forwarder_org_id)
            .where(PlanForwarderRow.plan_id.in_(plan_ids))
            .order_by(PlanForwarderRow.created_at, OrganizationRow.id)
        ).all()
    found: dict[str, ForwarderRef] = {}
    for plan_id, org_id, name in rows:
        found.setdefault(plan_id, ForwarderRef(org_id=org_id, org_name=name))
    return found


def _token_pattern(token: str) -> str:
    escaped = re.escape(token)
    if token.isascii():
        return f"(?<![A-Za-z0-9_]){escaped}(?![A-Za-z0-9_])"
    return escaped


LotLedger = Mapping[str, str | None]


def _note_lot(ledger: dict[str, str | None], lot_id: Any, owner: Any) -> None:
    if not isinstance(lot_id, str) or not lot_id:
        return
    if ledger.get(lot_id) is None:
        ledger[lot_id] = owner if isinstance(owner, str) and owner else None


def _collect_lots(value: Any, ledger: dict[str, str | None]) -> None:
    if isinstance(value, list):
        for item in value:
            _collect_lots(item, ledger)
        return
    if not isinstance(value, dict):
        return
    owner = value.get("shipper_id") or value.get("payer_shipper_id")
    for key, item in value.items():
        if key == "lot" and isinstance(item, dict):
            _note_lot(ledger, item.get("id"), item.get("shipper_id"))
            continue
        if key == "lot_id":
            _note_lot(ledger, item, owner)
        elif key.endswith("lot_ids") and isinstance(item, list):
            for lot_id in item:
                _note_lot(ledger, lot_id, owner if key == "lot_ids" else None)
        elif key == "dropped_stops" and isinstance(item, dict):
            for lot_id in item:
                _note_lot(ledger, lot_id, None)
        else:
            _collect_lots(item, ledger)


def _stored_lot_shippers(lot_ids: list[str]) -> dict[str, str]:
    if not lot_ids:
        return {}
    with database.begin() as db:
        rows = db.execute(select(LotRow.id, LotRow.shipper_id).where(LotRow.id.in_(lot_ids)))
        return {lot_id: shipper_id for lot_id, shipper_id in rows}


def lot_ledger(
    session: PlanSession,
    orders: Iterable[ExecutionOrder] = (),
    own_shipper_ids: Iterable[str] = (),
) -> dict[str, str | None]:
    own = set(own_shipper_ids)
    ledger: dict[str, str | None] = dict.fromkeys(session.registered_lot_ids)
    for entry in sorted(session.history, key=lambda e: e.seq):
        if isinstance(entry.event, AddLotEvent):
            _note_lot(ledger, entry.event.lot.id, entry.event.lot.shipper_id)
    for lot in session.scenario.lots:
        ledger[lot.id] = lot.shipper_id
    _collect_lots(session.model_dump(mode="json"), ledger)
    for order in orders:
        _collect_lots(order.model_dump(mode="json"), ledger)
    unresolved = [lot_id for lot_id, owner in ledger.items() if owner is None]
    for lot_id, owner in _stored_lot_shippers(unresolved).items():
        if owner not in own:
            ledger[lot_id] = owner
    return ledger


def _scenario_ledger(scenario: Scenario) -> dict[str, str | None]:
    return {lot.id: lot.shipper_id for lot in scenario.lots}


class Anonymizer:
    def __init__(
        self,
        scenario: Scenario,
        own_shipper_ids: Iterable[str],
        lots: LotLedger | None = None,
    ) -> None:
        own = set(own_shipper_ids)
        ledger = _scenario_ledger(scenario) if lots is None else dict(lots)
        own_packhouses = {s.packhouse_id for s in scenario.shippers if s.id in own}
        packhouses = {p.id: p for p in scenario.packhouses}
        listed = [s.id for s in scenario.shippers]
        with_lots = {sid for sid in ledger.values() if sid is not None}
        shipper_ids = [sid for sid in listed if sid in with_lots]
        shipper_ids += sorted(with_lots - set(listed))
        shipper_ids += [sid for sid in listed if sid not in with_lots]
        names = {s.id: s for s in scenario.shippers}
        own_shorts = {short_name(s.name) for s in scenario.shippers if s.id in own}
        tokens: dict[str, str] = {}
        self.hidden_packhouses: set[str] = set()
        self.own_lot_ids = {lot_id for lot_id, sid in ledger.items() if sid in own}
        others = [sid for sid in shipper_ids if sid not in own]
        labels: dict[str | None, str] = {None: UNKNOWN_OWNER_LABEL}
        for index, sid in enumerate(others):
            label = _label(index)
            labels[sid] = label
            tokens[sid] = f"anon-{label}"
            shipper = names.get(sid)
            if shipper is not None:
                tokens[shipper.name] = f"{OTHER_SHIPPER_LABEL} {label}"
                short = short_name(shipper.name)
                if len(short) >= 2 and short not in own_shorts:
                    tokens.setdefault(short, f"{OTHER_SHIPPER_LABEL} {label}")
                ph = packhouses.get(shipper.packhouse_id)
                if shipper.packhouse_id not in own_packhouses:
                    anon_ph = f"anon-ph-{label}"
                    tokens[shipper.packhouse_id] = anon_ph
                    self.hidden_packhouses.add(anon_ph)
                    if ph is not None:
                        tokens[ph.name] = f"{OTHER_SHIPPER_LABEL} {label} 선과장"
        counts: dict[str, int] = {}
        for lot_id, sid in ledger.items():
            if lot_id in self.own_lot_ids:
                tokens[lot_id] = lot_id
                continue
            label = labels.get(sid, UNKNOWN_OWNER_LABEL)
            counts[label] = counts.get(label, 0) + 1
            tokens[lot_id] = f"anon-{label}-{counts[label]}"
        self._tokens = tokens
        ordered = sorted((t for t in tokens if t), key=len, reverse=True)
        self._pattern = re.compile("|".join(map(_token_pattern, ordered))) if ordered else None

    def text(self, value: str) -> str:
        if self._pattern is None:
            return value
        return self._pattern.sub(lambda m: self._tokens[m.group(0)], value)

    def walk(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, list):
            return [self.walk(item) for item in value]
        if isinstance(value, dict):
            return {self.text(k): self.walk(v) for k, v in value.items()}
        return value

    def session(self, payload: dict[str, Any]) -> dict[str, Any]:
        result = self.walk(payload)
        scenario = result.get("scenario") or {}
        if "description" in scenario:
            scenario["description"] = ""
        for packhouse in scenario.get("packhouses", []):
            if packhouse.get("id") in self.hidden_packhouses:
                packhouse.update(region="", lat=None, lon=None)
        return result


def shipper_anonymizer(
    session: PlanSession,
    own_shipper_ids: Iterable[str],
    orders: Iterable[ExecutionOrder] = (),
) -> Anonymizer:
    own = set(own_shipper_ids)
    return Anonymizer(session.scenario, own, lot_ledger(session, orders, own))


def _without_total_cost(summary: str) -> str:
    return TOTAL_COST_CLAUSE.sub("입니다.", summary)


def _scope_explanations(payload: dict[str, Any], own: set[str]) -> None:
    for entry in payload["history"]:
        explanation = entry.get("explanation")
        if explanation is None:
            continue
        explanation["operator_summary"] = _without_total_cost(explanation["operator_summary"])
        explanation["shipper_messages"] = [
            m for m in explanation["shipper_messages"] if m["shipper_id"] in own
        ]


def _cost_total(costs: list[dict[str, Any]], name: str) -> int:
    return sum((c.get(name) or {}).get("total", 0) for c in costs)


def _own_container(container: dict[str, Any], own: set[str]) -> dict[str, Any] | None:
    rows = [r for r in container["shippers"] if r["shipper_id"] in own]
    if not rows:
        return None
    return {
        **container,
        "shippers": rows,
        "total_savings": sum(r["savings"] for r in rows),
        "total_fee": sum(r["fee"] for r in rows),
        "total_shipper_gain": sum(r["shipper_gain"] for r in rows),
    }


def _scope_settlements(payload: dict[str, Any], own: set[str]) -> None:
    plan = payload["plan"]
    rows = [s for s in plan["settlements"] if s["shipper_id"] in own]
    costs = [c for c in plan["shipper_costs"] if c["shipper_id"] in own]
    containers = [_own_container(c, own) for c in plan["container_settlements"]]
    operational_baseline = _cost_total(costs, "operational_baseline")
    operational = _cost_total(costs, "operational")
    collections = [c for c in plan["lot_collections"] if c["shipper_id"] in own]
    if plan["total_collection_route"] is not None:
        plan["total_collection_route"] = sum(
            (c.get("operational") or {}).get("collection", 0) for c in costs
        )
    plan.update(
        lot_collections=collections,
        total_collection_estimate=sum(
            (c.get("operational") or {}).get("collection_estimate") or 0 for c in costs
        ),
        settlements=rows,
        shipper_costs=costs,
        container_settlements=[c for c in containers if c is not None],
        total_baseline_cost=sum(s["baseline_cost"] for s in rows),
        total_shared_cost=sum(s["shared_cost"] for s in rows),
        total_savings=sum(s["savings"] for s in rows),
        total_fee=sum(s["fee"] for s in rows),
        total_operational_baseline_cost=operational_baseline,
        total_operational_cost=operational,
        total_operational_savings=operational_baseline - operational,
        objective=None,
    )
    for entry in payload["history"]:
        for name in HISTORY_MONEY:
            entry[name] = None


def _operational_inspections(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {**{name: row[name] for name in INSPECTION_FIELDS}, "excess_loss_value": None, "note": None}
        for row in rows
    ]


def _scope_tariffs(payload: dict[str, Any], session: PlanSession, own: set[str]) -> None:
    products = {lot.product_id for lot in session.scenario.lots if lot.shipper_id in own}
    plan = payload["plan"]
    plan["settlement_tariffs"] = [
        t for t in plan["settlement_tariffs"] if t["product_id"] in products
    ]


def _exclusive(route: dict[str, Any], own_lots: set[str]) -> bool:
    lot_ids = {lot_id for stop in route["stops"] for lot_id in stop["lot_ids"]}
    return bool(lot_ids) and lot_ids <= own_lots


def _allowed(item: dict[str, Any], fields: frozenset[str]) -> dict[str, Any]:
    return {key: value if key in fields else None for key, value in item.items()}


def _hidden_route(route: dict[str, Any]) -> dict[str, Any]:
    scoped = _allowed(route, ROUTE_FIELDS)
    scoped["stops"] = [_allowed(stop, STOP_FIELDS) for stop in route["stops"]]
    return scoped


def _scope_routes(payload: dict[str, Any], own_lots: set[str]) -> None:
    for voyage in payload["routes"]:
        pickup = voyage["pickup"]
        routes = pickup["routes"]
        if all(_exclusive(route, own_lots) for route in routes):
            continue
        voyage["pickup"] = {
            **_allowed(pickup, PICKUP_FIELDS),
            "routes": [
                route if _exclusive(route, own_lots) else _hidden_route(route) for route in routes
            ],
        }


def anonymized_session(
    session: PlanSession,
    own_shipper_ids: Iterable[str],
    orders: Iterable[ExecutionOrder] = (),
) -> dict[str, Any]:
    own = set(own_shipper_ids)
    anonymizer = shipper_anonymizer(session, own, orders)
    payload = session.model_dump(mode="json")
    _scope_settlements(payload, own)
    _scope_tariffs(payload, session, own)
    _scope_explanations(payload, own)
    _scope_routes(payload, anonymizer.own_lot_ids)
    payload["arrival_inspections"] = [
        row for row in payload["arrival_inspections"] if row["shipper_id"] in own
    ]
    return anonymizer.session(payload)


def shipper_lot_ids(session: PlanSession, own_shipper_ids: Iterable[str]) -> set[str]:
    own = set(own_shipper_ids)
    return {lot.id for lot in session.scenario.lots if lot.shipper_id in own}


def related_to_shipper(order: ExecutionOrder, own: set[str], my_lots: set[str]) -> bool:
    shippers = {c.shipper_id for c in order.shipper_confirmations} | set(order.notified_shipper_ids)
    return bool(own & shippers) or any(my_lots & set(r.lot_ids) for r in order.requests)


def _order_view(
    anonymizer: Anonymizer, order: ExecutionOrder, own: set[str], my_lots: set[str]
) -> dict[str, Any]:
    trimmed = order.model_copy(
        update={
            "shipper_confirmations": [
                c for c in order.shipper_confirmations if c.shipper_id in own
            ],
            "notified_shipper_ids": [s for s in order.notified_shipper_ids if s in own],
            "requests": [r for r in order.requests if my_lots & set(r.lot_ids)],
        }
    )
    payload = trimmed.model_dump(mode="json")
    for action in payload["automatic"]:
        if action["kind"] == SETTLEMENT_ACTION:
            action["description"] = _own_settlement_text(order.cancellation, own)
    cancellation = payload.get("cancellation")
    if cancellation is not None:
        if cancellation["payer_shipper_id"] not in own:
            cancellation["fee"] = None
            cancellation["consolidator_amount"] = None
        cancellation["transfers"] = [t for t in cancellation["transfers"] if t["shipper_id"] in own]
    return anonymizer.walk(payload)


def shipper_order_view(
    session: PlanSession,
    order: ExecutionOrder,
    own_shipper_ids: Iterable[str],
    orders: Iterable[ExecutionOrder] = (),
) -> dict[str, Any]:
    own = set(own_shipper_ids)
    known = [*orders]
    if all(o.order_id != order.order_id for o in known):
        known.append(order)
    anonymizer = shipper_anonymizer(session, own, known)
    my_lots = shipper_lot_ids(session, own) | anonymizer.own_lot_ids
    return _order_view(anonymizer, order, own, my_lots)


def _own_settlement_text(charge: CancellationCharge | None, own: set[str]) -> str:
    parts = [SETTLEMENT_TITLE]
    if charge is not None and charge.fee > 0:
        if charge.payer_shipper_id in own:
            parts.append(f"취소 부담금 {charge.fee:,}원 부담")
        parts += [
            f"취소 부담금 {t.amount:,}원 보전" for t in charge.transfers if t.shipper_id in own
        ]
    return ". ".join(parts)


def anonymized_orders(
    session: PlanSession, orders: list[ExecutionOrder], own_shipper_ids: Iterable[str]
) -> list[dict[str, Any]]:
    own = set(own_shipper_ids)
    anonymizer = shipper_anonymizer(session, own, orders)
    my_lots = shipper_lot_ids(session, own) | anonymizer.own_lot_ids
    return [
        _order_view(anonymizer, order, own, my_lots)
        for order in orders
        if related_to_shipper(order, own, my_lots)
    ]


def _own_packhouses(session: PlanSession, own: set[str]) -> set[str]:
    return {s.packhouse_id for s in session.scenario.shippers if s.id in own}


def shipper_route_changes(
    session: PlanSession,
    changes: Iterable[RouteChange],
    own_shipper_ids: Iterable[str],
    orders: Iterable[ExecutionOrder] = (),
) -> list[ShipperRouteChange]:
    own = set(own_shipper_ids)
    anonymizer = shipper_anonymizer(session, own, orders)
    own_lots = shipper_lot_ids(session, own) | anonymizer.own_lot_ids
    own_packhouses = _own_packhouses(session, own)
    scoped: list[ShipperRouteChange] = []
    for change in changes:
        lots = set(change.lot_ids_before) | set(change.lot_ids_after)
        stops = set(change.packhouses_before) | set(change.packhouses_after)
        if not (lots & own_lots or stops & own_packhouses):
            continue
        item = change.model_dump(mode="json")
        for name in ROUTE_CHANGE_PACKHOUSES:
            item[name] = [ph for ph in item[name] if ph in own_packhouses]
        for name in ROUTE_CHANGE_LOTS:
            item[name] = [lot_id for lot_id in item[name] if lot_id in own_lots]
        for name in ROUTE_CHANGE_MONEY:
            item[name] = None
        scoped.append(ShipperRouteChange.model_validate(anonymizer.walk(item)))
    return scoped


def shipper_fixed_reasons(
    session: PlanSession,
    reasons: Iterable[FixedLot],
    own_shipper_ids: Iterable[str],
    orders: Iterable[ExecutionOrder] = (),
) -> list[FixedLot]:
    own = set(own_shipper_ids)
    anonymizer = shipper_anonymizer(session, own, orders)
    own_lots = shipper_lot_ids(session, own) | anonymizer.own_lot_ids
    return [
        FixedLot.model_validate(anonymizer.walk(reason.model_dump(mode="json")))
        for reason in reasons
        if reason.lot_id in own_lots
    ]


def without_settlement(session: PlanSession) -> dict[str, Any]:
    payload = session.model_dump(mode="json")
    plan = payload["plan"]
    for name in MONEY_LISTS:
        plan[name] = []
    for name in MONEY_TOTALS:
        plan[name] = None
    plan["objective"] = None
    for entry in payload["history"]:
        for name in HISTORY_MONEY:
            entry[name] = None
    _scope_explanations(payload, set())
    payload["arrival_inspections"] = _operational_inspections(payload["arrival_inspections"])
    return payload


def forwarder_order_view(order: ExecutionOrder) -> ForwarderOrder:
    payload = order.model_dump(mode="json")
    for action in payload["automatic"]:
        if action["kind"] == SETTLEMENT_ACTION:
            action["description"] = SETTLEMENT_TITLE
    for confirmation in payload["shipper_confirmations"]:
        for name in CONFIRMATION_MONEY:
            confirmation[name] = None
    cancellation = payload.get("cancellation")
    if cancellation is not None:
        for name in CANCELLATION_MONEY:
            cancellation[name] = None
        cancellation["transfers"] = [
            {**transfer, "share_increase": None, "amount": None}
            for transfer in cancellation["transfers"]
        ]
    return ForwarderOrder.model_validate(payload)
