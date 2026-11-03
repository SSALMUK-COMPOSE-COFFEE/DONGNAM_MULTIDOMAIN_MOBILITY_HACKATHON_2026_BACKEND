import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, exists, func, select, text, true, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from freshswitch_backend.domain.models import Lot
from freshswitch_backend.lots.models import LotFilter, RegisteredLot
from freshswitch_backend.persistence import Database, database
from freshswitch_backend.persistence.tables import DEMO_ORG, LotRow, PlanRow

ANY_ORG = "*"
LOT_ID_LOCK = 7_310_001
PLAN_LOT_IDS = text(
    "SELECT DISTINCT lot->>'id' FROM plan_sessions, "
    "jsonb_array_elements(payload->'scenario'->'lots') AS lot"
)


class LotConflict(ValueError):
    pass


@dataclass
class Reservation:
    taken: set[str]
    reserved: set[str]


def mark_planned(db: Session, lot_ids: list[str], now: datetime, org_id: str) -> None:
    if lot_ids:
        db.execute(
            update(LotRow)
            .where(LotRow.id.in_(lot_ids), LotRow.status == "registered", _lot_scope(org_id))
            .values(status="planned", updated_at=now)
        )


def release_lots(
    db: Session, lot_ids: set[str] | None, now: datetime, org_id: str = ANY_ORG
) -> None:
    if lot_ids is not None and not lot_ids:
        return
    referenced = exists().where(
        PlanRow.payload["registered_lot_ids"].contains(func.jsonb_build_array(LotRow.id))
    )
    statement = update(LotRow).where(LotRow.status == "planned", ~referenced)
    if lot_ids is not None:
        statement = statement.where(LotRow.id.in_(lot_ids))
    statement = statement.where(_lot_scope(org_id))
    db.execute(statement.values(status="registered", updated_at=now))


def sync_committed_lots(db: Session, lot_ids: list[str], lots: list[Lot], now: datetime) -> None:
    registered = set(lot_ids)
    if not registered:
        return
    pallets = {lot.id: lot.pallets for lot in lots if lot.id in registered}
    for lot_id, count in pallets.items():
        db.execute(
            update(LotRow)
            .where(LotRow.id == lot_id, LotRow.status == "planned", LotRow.pallets != count)
            .values(pallets=count, updated_at=now)
        )
    removed = registered - set(pallets)
    if removed:
        db.execute(
            update(LotRow)
            .where(LotRow.id.in_(removed), LotRow.status == "planned")
            .values(status="cancelled", updated_at=now)
        )


def _now() -> datetime:
    return datetime.now(UTC)


def _lot_scope(org_id: str) -> Any:
    return true() if org_id == ANY_ORG else LotRow.shipper_org_id == org_id


def _model(row: LotRow) -> RegisteredLot:
    return RegisteredLot.model_validate(row, from_attributes=True)


class LotStore:
    def __init__(self, db: Database | None = None) -> None:
        self._db = db or database
        self._lock = threading.RLock()
        self.clock: Callable[[], datetime] = _now

    def _row(self, lot: Lot, scenario_id: str, packhouse: str | None, org_id: str) -> LotRow:
        now = self.clock()
        return LotRow(
            id=lot.id,
            scenario_id=scenario_id,
            shipper_id=lot.shipper_id,
            shipper_org_id=org_id,
            product_id=lot.product_id,
            pallets=lot.pallets,
            ready_date=lot.ready_date,
            destination=lot.destination,
            sell_by_date=lot.sell_by_date,
            sell_window=lot.sell_window,
            packhouse=packhouse,
            source=lot.source,
            status="registered",
            created_at=now,
            updated_at=now,
        )

    def _reservation(self, db: Session) -> Reservation:
        return Reservation(
            taken=set(db.scalars(select(LotRow.id))),
            reserved={i for i in db.scalars(PLAN_LOT_IDS) if i},
        )

    def reservation(self) -> Reservation:
        with self._db.begin() as db:
            return self._reservation(db)

    def create(
        self,
        build: Callable[[Reservation], tuple[Lot, str, str | None]],
        org_id: str = DEMO_ORG,
    ) -> RegisteredLot:
        try:
            with self._lock, self._db.begin() as db:
                db.execute(select(func.pg_advisory_xact_lock(LOT_ID_LOCK)))
                lot, scenario_id, packhouse = build(self._reservation(db))
                row = self._row(lot, scenario_id, packhouse, org_id)
                db.add(row)
                db.flush()
        except IntegrityError as exc:
            raise LotConflict(f"이미 있는 로트 ID입니다: {lot.id}") from exc
        return _model(row)

    def get(self, lot_id: str, org_id: str = DEMO_ORG) -> RegisteredLot | None:
        with self._db.begin() as db:
            row = db.scalars(select(LotRow).where(LotRow.id == lot_id, _lot_scope(org_id))).first()
            return _model(row) if row is not None else None

    def get_many(self, lot_ids: list[str], org_id: str = DEMO_ORG) -> list[RegisteredLot]:
        if not lot_ids:
            return []
        with self._db.begin() as db:
            rows = db.scalars(select(LotRow).where(LotRow.id.in_(lot_ids), _lot_scope(org_id)))
            found = {row.id: _model(row) for row in rows}
        return [found[i] for i in lot_ids if i in found]

    def ids(self, org_id: str = DEMO_ORG) -> set[str]:
        with self._db.begin() as db:
            return set(db.scalars(select(LotRow.id).where(_lot_scope(org_id))))

    def org_of(self, lot_id: str) -> str | None:
        with self._db.begin() as db:
            return db.scalar(select(LotRow.shipper_org_id).where(LotRow.id == lot_id))

    def search(self, query: LotFilter, org_id: str = DEMO_ORG) -> list[RegisteredLot]:
        statement = select(LotRow).where(_lot_scope(org_id))
        conditions = [
            (query.scenario_id, LotRow.scenario_id),
            (query.shipper, LotRow.shipper_id),
            (query.product, LotRow.product_id),
            (query.destination, LotRow.destination),
            (query.status, LotRow.status),
        ]
        for value, column in conditions:
            if value is not None:
                statement = statement.where(column == value)
        if query.ready_from is not None:
            statement = statement.where(LotRow.ready_date >= query.ready_from)
        if query.ready_to is not None:
            statement = statement.where(LotRow.ready_date <= query.ready_to)
        statement = (
            statement.order_by(LotRow.created_at.desc(), LotRow.id)
            .limit(query.limit)
            .offset(query.offset)
        )
        with self._db.begin() as db:
            return [_model(row) for row in db.scalars(statement)]

    def update(
        self,
        lot_id: str,
        change: Callable[[RegisteredLot], dict[str, Any]],
        org_id: str = DEMO_ORG,
    ) -> RegisteredLot | None:
        with self._lock, self._db.begin() as db:
            row = db.get(LotRow, lot_id, with_for_update=True)
            if row is None or (org_id != ANY_ORG and row.shipper_org_id != org_id):
                return None
            for name, value in change(_model(row)).items():
                setattr(row, name, value)
            row.updated_at = self.clock()
            db.flush()
            return _model(row)

    def delete(self, lot_id: str, org_id: str = DEMO_ORG) -> bool:
        with self._lock, self._db.begin() as db:
            statement = delete(LotRow).where(LotRow.id == lot_id, _lot_scope(org_id))
            return db.execute(statement).rowcount > 0

    def clear(self, org_id: str = DEMO_ORG) -> int:
        with self._lock, self._db.begin() as db:
            return db.execute(delete(LotRow).where(LotRow.shipper_org_id == org_id)).rowcount


lot_store = LotStore()
