from datetime import date, datetime
from typing import Any, ClassVar

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Sequence,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

Json = dict[str, Any]

DEMO_ORG = "demo"
ORG_TYPES = ("platform", "shipper", "forwarder", "demo")
MEMBERSHIP_ROLES = ("admin", "member")


class Base(DeclarativeBase):
    type_annotation_map: ClassVar[dict[Any, Any]] = {
        Json: JSONB,
        list[Any]: JSONB,
        datetime: DateTime(timezone=True),
        date: Date,
    }


ALERT_SEQ = Sequence("coldchain_alert_seq", metadata=Base.metadata)


def _org_fk(ondelete: str = "RESTRICT") -> ForeignKey:
    return ForeignKey("organizations.id", ondelete=ondelete)


def _org_column() -> Mapped[str]:
    return mapped_column(String(64), _org_fk(), index=True, server_default=DEMO_ORG)


def _user_fk(ondelete: str = "CASCADE") -> ForeignKey:
    return ForeignKey("users.id", ondelete=ondelete)


class MetaRow(Base):
    __tablename__ = "app_meta"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[Json]
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class OrganizationRow(Base):
    __tablename__ = "organizations"
    __table_args__ = (
        CheckConstraint(
            "type IN ('platform', 'shipper', 'forwarder', 'demo')",
            name="organizations_type_check",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    type: Mapped[str] = mapped_column(String(20), index=True)
    name: Mapped[str] = mapped_column(String(200))
    sandbox: Mapped[bool] = mapped_column(server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class UserRow(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    name: Mapped[str] = mapped_column(String(200))
    is_demo: Mapped[bool] = mapped_column(server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class MembershipRow(Base):
    __tablename__ = "memberships"
    __table_args__ = (
        CheckConstraint("role IN ('admin', 'member')", name="memberships_role_check"),
    )

    user_id: Mapped[str] = mapped_column(String(64), _user_fk(), primary_key=True)
    org_id: Mapped[str] = mapped_column(
        String(64), _org_fk("CASCADE"), primary_key=True, index=True
    )
    role: Mapped[str] = mapped_column(String(20), server_default="member")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class AuthSessionRow(Base):
    __tablename__ = "auth_sessions"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), _user_fk(), index=True)
    active_org_id: Mapped[str | None] = mapped_column(String(64), _org_fk("SET NULL"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(index=True)
    last_seen_at: Mapped[datetime | None]


class PackhouseRow(Base):
    __tablename__ = "packhouses"

    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    org_id: Mapped[str] = mapped_column(String(64), _org_fk("CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    region: Mapped[str] = mapped_column(String(100))
    lat: Mapped[float] = mapped_column(Float)
    lon: Mapped[float] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(200), server_default="등록")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class ShipperProfileRow(Base):
    __tablename__ = "shipper_profiles"

    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    org_id: Mapped[str] = mapped_column(String(64), _org_fk("CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    packhouse_id: Mapped[str] = mapped_column(
        String(100), ForeignKey("packhouses.id", ondelete="RESTRICT"), index=True
    )
    product_ids: Mapped[list[Any]] = mapped_column(server_default=text("'[]'::jsonb"))
    source: Mapped[str] = mapped_column(String(200), server_default="등록")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class DeviceRow(Base):
    __tablename__ = "devices"

    device_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    org_id: Mapped[str] = mapped_column(String(64), _org_fk("CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    last_seen_at: Mapped[datetime | None]


class LotRow(Base):
    __tablename__ = "lots"

    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    scenario_id: Mapped[str] = mapped_column(String(100), index=True)
    shipper_id: Mapped[str] = mapped_column(String(100), index=True)
    shipper_org_id: Mapped[str] = _org_column()
    product_id: Mapped[str] = mapped_column(String(100), index=True)
    pallets: Mapped[int] = mapped_column(Integer)
    ready_date: Mapped[date] = mapped_column(index=True)
    destination: Mapped[str] = mapped_column(String(100), index=True)
    sell_by_date: Mapped[date]
    sell_window: Mapped[str] = mapped_column(String(20), default="normal")
    packhouse: Mapped[str | None] = mapped_column(String(100))
    source: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20), index=True)
    created_at: Mapped[datetime] = mapped_column(index=True)
    updated_at: Mapped[datetime]


class PlanRow(Base):
    __tablename__ = "plan_sessions"

    plan_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    seq: Mapped[int] = mapped_column(BigInteger, Identity(), unique=True)
    org_id: Mapped[str] = _org_column()
    scenario_id: Mapped[str] = mapped_column(String(100), index=True)
    version: Mapped[int]
    status: Mapped[str] = mapped_column(String(40))
    lot_count: Mapped[int]
    totals: Mapped[Json]
    created_at: Mapped[datetime] = mapped_column(index=True)
    updated_at: Mapped[datetime]
    payload: Mapped[Json]
    committed: Mapped[Json]


def _plan_fk() -> ForeignKey:
    return ForeignKey("plan_sessions.plan_id", ondelete="CASCADE")


class PlanForwarderRow(Base):
    __tablename__ = "plan_forwarders"

    plan_id: Mapped[str] = mapped_column(String(64), _plan_fk(), primary_key=True)
    forwarder_org_id: Mapped[str] = mapped_column(
        String(64), _org_fk("CASCADE"), primary_key=True, index=True
    )
    assigned_by: Mapped[str | None] = mapped_column(String(64), _user_fk("SET NULL"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class PendingRow(Base):
    __tablename__ = "plan_pending_replans"

    replan_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    seq: Mapped[int] = mapped_column(BigInteger, Identity(), unique=True)
    plan_id: Mapped[str] = mapped_column(String(64), _plan_fk(), index=True)
    base_version: Mapped[int]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    payload: Mapped[Json]


class OrderRow(Base):
    __tablename__ = "plan_orders"

    order_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    seq: Mapped[int] = mapped_column(BigInteger, Identity(), unique=True)
    plan_id: Mapped[str] = mapped_column(String(64), _plan_fk(), index=True)
    status: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]
    payload: Mapped[Json]


class IdempotencyRow(Base):
    __tablename__ = "plan_idempotency_keys"

    plan_id: Mapped[str] = mapped_column(String(64), _plan_fk(), primary_key=True)
    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    seq: Mapped[int] = mapped_column(BigInteger, Identity(), unique=True)
    kind: Mapped[str] = mapped_column(String(40))
    fingerprint: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    payload: Mapped[Json]


class ContainerRow(Base):
    __tablename__ = "coldchain_containers"

    record_key: Mapped[str] = mapped_column(String(180), primary_key=True)
    seq: Mapped[int] = mapped_column(BigInteger, Identity(), unique=True)
    org_id: Mapped[str] = _org_column()
    container_id: Mapped[str] = mapped_column(String(100), index=True)
    plan_id: Mapped[str | None] = mapped_column(String(64), index=True)
    stage: Mapped[str] = mapped_column(String(20))
    carry: Mapped[Json | None]
    history: Mapped[list[Any]]
    anchors: Mapped[list[Any]]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


def _container_fk() -> ForeignKey:
    return ForeignKey("coldchain_containers.record_key", ondelete="CASCADE")


class ReadingRow(Base):
    __tablename__ = "coldchain_readings"
    __table_args__ = (Index("ix_coldchain_readings_record_ts", "record_key", "ts"),)

    record_key: Mapped[str] = mapped_column(String(180), _container_fk(), primary_key=True)
    device_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    ts: Mapped[datetime] = mapped_column(primary_key=True)
    payload: Mapped[Json]


class AlertRow(Base):
    __tablename__ = "coldchain_alerts"
    __table_args__ = (UniqueConstraint("record_key", "key"),)

    seq: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    org_id: Mapped[str] = _org_column()
    record_key: Mapped[str] = mapped_column(String(180), _container_fk(), index=True)
    container_id: Mapped[str] = mapped_column(String(100), index=True)
    plan_id: Mapped[str | None] = mapped_column(String(64), index=True)
    key: Mapped[str]
    created_at: Mapped[datetime]
    payload: Mapped[Json]


class BindingRow(Base):
    __tablename__ = "coldchain_bindings"

    device_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    org_id: Mapped[str] = _org_column()
    container_id: Mapped[str] = mapped_column(String(100), index=True)
    plan_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    payload: Mapped[Json]


class RunRow(Base):
    __tablename__ = "coldchain_sim_runs"

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    seq: Mapped[int] = mapped_column(BigInteger, Identity(), unique=True)
    org_id: Mapped[str] = _org_column()
    container_id: Mapped[str] = mapped_column(String(100), index=True)
    done: Mapped[bool]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())
    payload: Mapped[Json]


class ObservationRow(Base):
    __tablename__ = "quality_observations"
    __table_args__ = (Index("ix_quality_observations_plan_lot", "plan_id", "lot_id"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    seq: Mapped[int] = mapped_column(BigInteger, Identity(), unique=True)
    origin: Mapped[str] = mapped_column(String(20), index=True)
    product_id: Mapped[str] = mapped_column(String(100), index=True)
    plan_id: Mapped[str | None] = mapped_column(String(64))
    lot_id: Mapped[str | None] = mapped_column(String(120))
    recorded_at: Mapped[datetime]
    inspection_revision: Mapped[int | None]
    payload: Mapped[Json]
