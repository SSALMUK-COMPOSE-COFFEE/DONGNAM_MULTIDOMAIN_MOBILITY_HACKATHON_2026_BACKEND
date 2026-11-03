"""initial schema"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(sa.schema.CreateSequence(sa.Sequence("coldchain_alert_seq")))
    op.create_table(
        "app_meta",
        sa.Column("key", sa.String(length=100), nullable=False),
        sa.Column("value", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("key"),
    )
    op.create_table(
        "coldchain_bindings",
        sa.Column("device_id", sa.String(length=100), nullable=False),
        sa.Column("container_id", sa.String(length=100), nullable=False),
        sa.Column("plan_id", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.PrimaryKeyConstraint("device_id"),
    )
    op.create_index(
        op.f("ix_coldchain_bindings_container_id"),
        "coldchain_bindings",
        ["container_id"],
        unique=False,
    )
    op.create_table(
        "coldchain_containers",
        sa.Column("record_key", sa.String(length=180), nullable=False),
        sa.Column("seq", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("container_id", sa.String(length=100), nullable=False),
        sa.Column("plan_id", sa.String(length=64), nullable=True),
        sa.Column("stage", sa.String(length=20), nullable=False),
        sa.Column("carry", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("history", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("anchors", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("record_key"),
        sa.UniqueConstraint("seq"),
    )
    op.create_index(
        op.f("ix_coldchain_containers_container_id"),
        "coldchain_containers",
        ["container_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_coldchain_containers_plan_id"), "coldchain_containers", ["plan_id"], unique=False
    )
    op.create_table(
        "coldchain_sim_runs",
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("seq", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("container_id", sa.String(length=100), nullable=False),
        sa.Column("done", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.PrimaryKeyConstraint("run_id"),
        sa.UniqueConstraint("seq"),
    )
    op.create_index(
        op.f("ix_coldchain_sim_runs_container_id"),
        "coldchain_sim_runs",
        ["container_id"],
        unique=False,
    )
    op.create_table(
        "lots",
        sa.Column("id", sa.String(length=120), nullable=False),
        sa.Column("scenario_id", sa.String(length=100), nullable=False),
        sa.Column("shipper_id", sa.String(length=100), nullable=False),
        sa.Column("product_id", sa.String(length=100), nullable=False),
        sa.Column("pallets", sa.Integer(), nullable=False),
        sa.Column("ready_date", sa.Date(), nullable=False),
        sa.Column("destination", sa.String(length=100), nullable=False),
        sa.Column("sell_by_date", sa.Date(), nullable=False),
        sa.Column("sell_window", sa.String(length=20), nullable=False),
        sa.Column("packhouse", sa.String(length=100), nullable=True),
        sa.Column("source", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_lots_created_at"), "lots", ["created_at"], unique=False)
    op.create_index(op.f("ix_lots_destination"), "lots", ["destination"], unique=False)
    op.create_index(op.f("ix_lots_product_id"), "lots", ["product_id"], unique=False)
    op.create_index(op.f("ix_lots_ready_date"), "lots", ["ready_date"], unique=False)
    op.create_index(op.f("ix_lots_scenario_id"), "lots", ["scenario_id"], unique=False)
    op.create_index(op.f("ix_lots_shipper_id"), "lots", ["shipper_id"], unique=False)
    op.create_index(op.f("ix_lots_status"), "lots", ["status"], unique=False)
    op.create_table(
        "plan_sessions",
        sa.Column("plan_id", sa.String(length=64), nullable=False),
        sa.Column("seq", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("scenario_id", sa.String(length=100), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("lot_count", sa.Integer(), nullable=False),
        sa.Column("totals", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("committed", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.PrimaryKeyConstraint("plan_id"),
        sa.UniqueConstraint("seq"),
    )
    op.create_index(
        op.f("ix_plan_sessions_created_at"), "plan_sessions", ["created_at"], unique=False
    )
    op.create_index(
        op.f("ix_plan_sessions_scenario_id"), "plan_sessions", ["scenario_id"], unique=False
    )
    op.create_table(
        "quality_observations",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("seq", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("origin", sa.String(length=20), nullable=False),
        sa.Column("product_id", sa.String(length=100), nullable=False),
        sa.Column("plan_id", sa.String(length=64), nullable=True),
        sa.Column("lot_id", sa.String(length=120), nullable=True),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("inspection_revision", sa.Integer(), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("seq"),
    )
    op.create_index(
        op.f("ix_quality_observations_origin"), "quality_observations", ["origin"], unique=False
    )
    op.create_index(
        "ix_quality_observations_plan_lot",
        "quality_observations",
        ["plan_id", "lot_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_quality_observations_product_id"),
        "quality_observations",
        ["product_id"],
        unique=False,
    )
    op.create_table(
        "coldchain_alerts",
        sa.Column("seq", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("record_key", sa.String(length=180), nullable=False),
        sa.Column("container_id", sa.String(length=100), nullable=False),
        sa.Column("plan_id", sa.String(length=64), nullable=True),
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.ForeignKeyConstraint(
            ["record_key"], ["coldchain_containers.record_key"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("seq"),
        sa.UniqueConstraint("record_key", "key"),
    )
    op.create_index(
        op.f("ix_coldchain_alerts_container_id"), "coldchain_alerts", ["container_id"], unique=False
    )
    op.create_index(
        op.f("ix_coldchain_alerts_plan_id"), "coldchain_alerts", ["plan_id"], unique=False
    )
    op.create_index(
        op.f("ix_coldchain_alerts_record_key"), "coldchain_alerts", ["record_key"], unique=False
    )
    op.create_table(
        "coldchain_readings",
        sa.Column("record_key", sa.String(length=180), nullable=False),
        sa.Column("device_id", sa.String(length=100), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.ForeignKeyConstraint(
            ["record_key"], ["coldchain_containers.record_key"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("record_key", "device_id", "ts"),
    )
    op.create_index(
        "ix_coldchain_readings_record_ts", "coldchain_readings", ["record_key", "ts"], unique=False
    )
    op.create_table(
        "plan_idempotency_keys",
        sa.Column("plan_id", sa.String(length=64), nullable=False),
        sa.Column("key", sa.String(length=200), nullable=False),
        sa.Column("seq", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("kind", sa.String(length=40), nullable=False),
        sa.Column("fingerprint", sa.String(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.ForeignKeyConstraint(["plan_id"], ["plan_sessions.plan_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("plan_id", "key"),
        sa.UniqueConstraint("seq"),
    )
    op.create_table(
        "plan_orders",
        sa.Column("order_id", sa.String(length=64), nullable=False),
        sa.Column("seq", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("plan_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.ForeignKeyConstraint(["plan_id"], ["plan_sessions.plan_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("order_id"),
        sa.UniqueConstraint("seq"),
    )
    op.create_index(op.f("ix_plan_orders_plan_id"), "plan_orders", ["plan_id"], unique=False)
    op.create_table(
        "plan_pending_replans",
        sa.Column("replan_id", sa.String(length=64), nullable=False),
        sa.Column("seq", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("plan_id", sa.String(length=64), nullable=False),
        sa.Column("base_version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.ForeignKeyConstraint(["plan_id"], ["plan_sessions.plan_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("replan_id"),
        sa.UniqueConstraint("seq"),
    )
    op.create_index(
        op.f("ix_plan_pending_replans_plan_id"), "plan_pending_replans", ["plan_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_plan_pending_replans_plan_id"), table_name="plan_pending_replans")
    op.drop_table("plan_pending_replans")
    op.drop_index(op.f("ix_plan_orders_plan_id"), table_name="plan_orders")
    op.drop_table("plan_orders")
    op.drop_table("plan_idempotency_keys")
    op.drop_index("ix_coldchain_readings_record_ts", table_name="coldchain_readings")
    op.drop_table("coldchain_readings")
    op.drop_index(op.f("ix_coldchain_alerts_record_key"), table_name="coldchain_alerts")
    op.drop_index(op.f("ix_coldchain_alerts_plan_id"), table_name="coldchain_alerts")
    op.drop_index(op.f("ix_coldchain_alerts_container_id"), table_name="coldchain_alerts")
    op.drop_table("coldchain_alerts")
    op.drop_index(op.f("ix_quality_observations_product_id"), table_name="quality_observations")
    op.drop_index("ix_quality_observations_plan_lot", table_name="quality_observations")
    op.drop_index(op.f("ix_quality_observations_origin"), table_name="quality_observations")
    op.drop_table("quality_observations")
    op.drop_index(op.f("ix_plan_sessions_scenario_id"), table_name="plan_sessions")
    op.drop_index(op.f("ix_plan_sessions_created_at"), table_name="plan_sessions")
    op.drop_table("plan_sessions")
    op.drop_index(op.f("ix_lots_status"), table_name="lots")
    op.drop_index(op.f("ix_lots_shipper_id"), table_name="lots")
    op.drop_index(op.f("ix_lots_scenario_id"), table_name="lots")
    op.drop_index(op.f("ix_lots_ready_date"), table_name="lots")
    op.drop_index(op.f("ix_lots_product_id"), table_name="lots")
    op.drop_index(op.f("ix_lots_destination"), table_name="lots")
    op.drop_index(op.f("ix_lots_created_at"), table_name="lots")
    op.drop_table("lots")
    op.drop_index(op.f("ix_coldchain_sim_runs_container_id"), table_name="coldchain_sim_runs")
    op.drop_table("coldchain_sim_runs")
    op.drop_index(op.f("ix_coldchain_containers_plan_id"), table_name="coldchain_containers")
    op.drop_index(op.f("ix_coldchain_containers_container_id"), table_name="coldchain_containers")
    op.drop_table("coldchain_containers")
    op.drop_index(op.f("ix_coldchain_bindings_container_id"), table_name="coldchain_bindings")
    op.drop_table("coldchain_bindings")
    op.drop_table("app_meta")
    op.execute(sa.schema.DropSequence(sa.Sequence("coldchain_alert_seq")))
