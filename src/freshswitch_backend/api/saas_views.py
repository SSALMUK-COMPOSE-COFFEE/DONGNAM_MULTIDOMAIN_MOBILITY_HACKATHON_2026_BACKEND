import re
from collections.abc import Iterable
from datetime import datetime
from typing import Any

from pydantic import BaseModel
from sqlalchemy import select

from freshswitch_backend.domain.models import Scenario
from freshswitch_backend.execution.models import ExecutionOrder
from freshswitch_backend.execution.orders import short_name
from freshswitch_backend.execution.quality import QualitySettlement, adjusted_settlements
from freshswitch_backend.persistence import database
from freshswitch_backend.persistence.tables import OrganizationRow, PlanForwarderRow
from freshswitch_backend.planning.store import PlanSession

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
CANCELLATION_MONEY = ("container_cost", "share_per_pallet_before", "share_per_pallet_after", "fee")


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


class PlatformSettlement(BaseModel):
    plan_id: str
    vessel: str | None
    departure: datetime | None
    total_saving_krw: int
    platform_fee_krw: int
    shippers: list[PlatformShipperShare]


def _label(index: int) -> str:
    letters = ""
    index += 1
    while index:
        index, rest = divmod(index - 1, 26)
        letters = chr(ord("A") + rest) + letters
    return letters


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
        shippers=[
            PlatformShipperShare(
                shipper_id=r.shipper_id,
                shipper_org_name=org_names.get(r.shipper_id),
                saving_krw=r.savings,
                fee_krw=r.fee,
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


class Anonymizer:
    def __init__(self, scenario: Scenario, own_shipper_ids: Iterable[str]) -> None:
        own = set(own_shipper_ids)
        own_packhouses = {s.packhouse_id for s in scenario.shippers if s.id in own}
        packhouses = {p.id: p for p in scenario.packhouses}
        shipper_ids = [s.id for s in scenario.shippers]
        shipper_ids += sorted({lot.shipper_id for lot in scenario.lots} - set(shipper_ids))
        names = {s.id: s for s in scenario.shippers}
        own_shorts = {short_name(s.name) for s in scenario.shippers if s.id in own}
        tokens: dict[str, str] = {}
        self.hidden_packhouses: set[str] = set()
        others = [sid for sid in shipper_ids if sid not in own]
        for index, sid in enumerate(others):
            label = _label(index)
            tokens[sid] = f"anon-{label}"
            shipper = names.get(sid)
            if shipper is not None:
                tokens[shipper.name] = f"화주 {label}"
                short = short_name(shipper.name)
                if len(short) >= 2 and short not in own_shorts:
                    tokens.setdefault(short, f"화주 {label}")
                ph = packhouses.get(shipper.packhouse_id)
                if shipper.packhouse_id not in own_packhouses:
                    anon_ph = f"anon-ph-{label}"
                    tokens[shipper.packhouse_id] = anon_ph
                    self.hidden_packhouses.add(anon_ph)
                    if ph is not None:
                        tokens[ph.name] = f"화주 {label} 선과장"
            count = 0
            for lot in scenario.lots:
                if lot.shipper_id == sid:
                    count += 1
                    tokens[lot.id] = f"anon-{label}-{count}"
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
        for packhouse in scenario.get("packhouses", []):
            if packhouse.get("id") in self.hidden_packhouses:
                packhouse.update(region="", lat=None, lon=None)
        return result


def anonymized_session(session: PlanSession, own_shipper_ids: Iterable[str]) -> dict[str, Any]:
    return Anonymizer(session.scenario, own_shipper_ids).session(session.model_dump(mode="json"))


def shipper_lot_ids(session: PlanSession, own_shipper_ids: Iterable[str]) -> set[str]:
    own = set(own_shipper_ids)
    return {lot.id for lot in session.scenario.lots if lot.shipper_id in own}


def related_to_shipper(order: ExecutionOrder, own: set[str], my_lots: set[str]) -> bool:
    shippers = {c.shipper_id for c in order.shipper_confirmations} | set(order.notified_shipper_ids)
    return bool(own & shippers) or any(my_lots & set(r.lot_ids) for r in order.requests)


def shipper_order_view(
    session: PlanSession, order: ExecutionOrder, own_shipper_ids: Iterable[str]
) -> dict[str, Any]:
    own = set(own_shipper_ids)
    my_lots = shipper_lot_ids(session, own)
    trimmed = order.model_copy(
        update={
            "shipper_confirmations": [
                c for c in order.shipper_confirmations if c.shipper_id in own
            ],
            "notified_shipper_ids": [s for s in order.notified_shipper_ids if s in own],
            "requests": [r for r in order.requests if my_lots & set(r.lot_ids)],
        }
    )
    return Anonymizer(session.scenario, own).walk(trimmed.model_dump(mode="json"))


def anonymized_orders(
    session: PlanSession, orders: list[ExecutionOrder], own_shipper_ids: Iterable[str]
) -> list[dict[str, Any]]:
    own = set(own_shipper_ids)
    my_lots = shipper_lot_ids(session, own)
    return [
        shipper_order_view(session, order, own)
        for order in orders
        if related_to_shipper(order, own, my_lots)
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
    return payload


def forwarder_order_view(order: ExecutionOrder) -> dict[str, Any]:
    payload = order.model_dump(mode="json")
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
    return payload
