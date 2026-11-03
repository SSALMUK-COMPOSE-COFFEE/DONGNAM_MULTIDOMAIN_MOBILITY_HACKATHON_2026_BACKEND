import math
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from freshswitch_backend.coldchain import coldchain_store
from freshswitch_backend.lots import lot_store
from freshswitch_backend.lots.store import release_lots
from freshswitch_backend.persistence.database import RESET_LOCK, database, get_meta, set_meta
from freshswitch_backend.persistence.seed import SCENARIO_ID, reset_to_baseline
from freshswitch_backend.persistence.tables import (
    AlertRow,
    AuthSessionRow,
    BindingRow,
    ContainerRow,
    DeviceRow,
    LotRow,
    MembershipRow,
    OrganizationRow,
    PlanRow,
    RunRow,
    UserRow,
)
from freshswitch_backend.quality import quality_history

SANDBOX_SCOPE = "sandbox"
RESET_COOLDOWN = timedelta(seconds=60)
RESET_META_KEY = "sandbox_reset_at"
DEMO_DEVICE_PREFIX = "demo-"


class SandboxResetCooldown(Exception):
    def __init__(self, retry_after: int) -> None:
        super().__init__(f"{retry_after}초 뒤에 다시 초기화할 수 있습니다")
        self.retry_after = retry_after


@dataclass(frozen=True)
class SandboxResetResult:
    orgs: int
    reset_at: datetime


def _now() -> datetime:
    return datetime.now(UTC)


clock: Callable[[], datetime] = _now
_lock = threading.Lock()


def sandbox_org_ids(db: Session) -> list[str]:
    return list(
        db.scalars(
            select(OrganizationRow.id)
            .where(OrganizationRow.sandbox.is_(True))
            .order_by(OrganizationRow.id)
        )
    )


@contextmanager
def _exclusive() -> Iterator[None]:
    with _lock, database.engine.connect() as conn:
        conn.execute(select(func.pg_advisory_lock(RESET_LOCK)))
        conn.commit()
        try:
            yield
        finally:
            conn.execute(select(func.pg_advisory_unlock(RESET_LOCK)))
            conn.commit()


def _check_cooldown(db: Session, now: datetime, cooldown: timedelta | None) -> None:
    last = get_meta(db, RESET_META_KEY)
    if cooldown is None or last is None:
        return
    remaining = cooldown - (now - datetime.fromisoformat(last))
    if remaining > timedelta(0):
        raise SandboxResetCooldown(max(1, math.ceil(remaining.total_seconds())))


def _logout(db: Session, keep_token_hash: str | None) -> None:
    demo_users = (
        select(MembershipRow.user_id)
        .join(OrganizationRow, OrganizationRow.id == MembershipRow.org_id)
        .join(UserRow, UserRow.id == MembershipRow.user_id)
        .where(OrganizationRow.sandbox.is_(True), UserRow.is_demo.is_(True))
    )
    sessions = delete(AuthSessionRow).where(AuthSessionRow.user_id.in_(demo_users))
    if keep_token_hash is not None:
        sessions = sessions.where(AuthSessionRow.token_hash != keep_token_hash)
    db.execute(sessions)


def _wipe(db: Session, org_ids: list[str]) -> None:
    db.execute(select(LotRow.id).where(LotRow.shipper_org_id.in_(org_ids)).with_for_update())
    db.execute(delete(AlertRow).where(AlertRow.org_id.in_(org_ids)))
    db.execute(delete(ContainerRow).where(ContainerRow.org_id.in_(org_ids)))
    db.execute(delete(BindingRow).where(BindingRow.org_id.in_(org_ids)))
    db.execute(delete(RunRow).where(RunRow.org_id.in_(org_ids)))
    db.execute(
        delete(DeviceRow).where(
            DeviceRow.org_id.in_(org_ids), DeviceRow.device_id.istartswith(DEMO_DEVICE_PREFIX)
        )
    )
    db.execute(delete(LotRow).where(LotRow.shipper_org_id.in_(org_ids)))


def _drop_plans(db: Session, org_ids: list[str], now: datetime) -> None:
    rows = db.execute(
        delete(PlanRow)
        .where(PlanRow.org_id.in_(org_ids))
        .returning(PlanRow.plan_id, PlanRow.payload["registered_lot_ids"])
    ).all()
    plan_ids = [row[0] for row in rows]
    if not plan_ids:
        return
    for binding in db.scalars(select(BindingRow).where(BindingRow.plan_id.in_(plan_ids))):
        binding.plan_id = None
        binding.payload = {**binding.payload, "plan_id": None}
    db.execute(delete(ContainerRow).where(ContainerRow.plan_id.in_(plan_ids)))
    release_lots(db, {lot_id for row in rows for lot_id in row[1] or []}, now)


def reset_sandbox(
    keep_token_hash: str | None = None,
    cooldown: timedelta | None = None,
    scenario_id: str = SCENARIO_ID,
) -> SandboxResetResult:
    with _exclusive():
        now = clock()
        with database.begin() as db:
            _check_cooldown(db, now, cooldown)
            org_ids = sandbox_org_ids(db)
            _logout(db, keep_token_hash)
        for org_id in org_ids:
            coldchain_store.clear(org_id)
        with database.begin() as db:
            stamp = lot_store.clock()
            if org_ids:
                _wipe(db, org_ids)
            reset_to_baseline(db, scenario_id, now=stamp)
            org_ids = sandbox_org_ids(db)
            _drop_plans(db, org_ids, stamp)
        quality_history.reset(scope=SANDBOX_SCOPE)
        with database.begin() as db:
            set_meta(db, RESET_META_KEY, now.isoformat())
        return SandboxResetResult(orgs=len(org_ids), reset_at=now)
