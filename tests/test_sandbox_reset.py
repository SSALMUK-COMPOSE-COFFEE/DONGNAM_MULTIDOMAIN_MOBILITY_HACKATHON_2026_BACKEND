from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from saas_fixtures import (
    FORWARDER_ORG_ID,
    PLATFORM_ORG_ID,
    REAL_PLATFORM_EMAIL,
    REAL_PLATFORM_PASSWORD,
    SAAS_CSRF,
    SHIPPER_ORG_ID,
    create_real_platform,
    login_client,
    password_client,
)
from sqlalchemy import func, select

from freshswitch_backend.config import get_settings
from freshswitch_backend.main import create_app
from freshswitch_backend.persistence import database
from freshswitch_backend.persistence import sandbox_reset as sandbox_reset_module
from freshswitch_backend.persistence.seed import main, run_lots
from freshswitch_backend.persistence.tables import (
    DEMO_ORG,
    AlertRow,
    AuthSessionRow,
    BindingRow,
    ContainerRow,
    DeviceRow,
    IdempotencyRow,
    LotRow,
    ObservationRow,
    OrderRow,
    OrganizationRow,
    PackhouseRow,
    PendingRow,
    PlanForwarderRow,
    PlanRow,
    ReadingRow,
    RunRow,
    ShipperProfileRow,
)
from freshswitch_backend.quality import quality_history
from freshswitch_backend.quality.models import ObservationIn

REAL_SHIPPER_ORG = "shipper-real"
REAL_LOT = "lot-real-1"
REAL_PLAN = "plan-real-1"
DEMO_LOT = "lot-demo-keep"
REAL_DEVICES = ("real-sensor-1", "demo-real-1")
STAMP = datetime(2026, 11, 10, tzinfo=UTC)
BASE_NOW = datetime(2026, 11, 10, 9, 0, tzinfo=UTC)


@pytest.fixture
def saas_app(demo_tenants):
    return create_app(get_settings().model_copy(update={"saas_mode": True}))


@pytest.fixture
def reset_clock(monkeypatch):
    now = [BASE_NOW]
    monkeypatch.setattr(sandbox_reset_module, "clock", lambda: now[0])
    return now


def _container(record_key: str, org_id: str, plan_id: str | None) -> ContainerRow:
    return ContainerRow(
        record_key=record_key,
        org_id=org_id,
        container_id=record_key,
        plan_id=plan_id,
        stage="loading",
        carry=None,
        history=[],
        anchors=[],
    )


def _coldchain(db, prefix: str, org_id: str, plan_id: str | None, device_id: str) -> None:
    db.add(_container(f"{prefix}-c", org_id, plan_id))
    db.flush()
    db.add(ReadingRow(record_key=f"{prefix}-c", device_id=device_id, ts=STAMP, payload={}))
    db.add(
        AlertRow(
            seq=db.scalar(select(func.nextval("coldchain_alert_seq"))),
            org_id=org_id,
            record_key=f"{prefix}-c",
            container_id=f"{prefix}-c",
            plan_id=plan_id,
            key=f"{prefix}-alert",
            created_at=STAMP,
            payload={},
        )
    )
    db.add(
        BindingRow(
            device_id=device_id,
            org_id=org_id,
            container_id=f"{prefix}-c",
            plan_id=plan_id,
            payload={},
        )
    )
    db.add(
        RunRow(
            run_id=f"{prefix}-run",
            org_id=org_id,
            container_id=f"{prefix}-c",
            done=True,
            payload={},
        )
    )


def _real_world() -> None:
    create_real_platform()
    with database.begin() as db:
        db.add(OrganizationRow(id=REAL_SHIPPER_ORG, type="shipper", name="실제 화주"))
        db.flush()
        db.add(
            PackhouseRow(
                id="ph-real",
                org_id=REAL_SHIPPER_ORG,
                name="실제 선과장",
                region="x",
                lat=35,
                lon=128,
            )
        )
        db.flush()
        db.add(
            ShipperProfileRow(
                id="sh-real", org_id=REAL_SHIPPER_ORG, name="실제 화주", packhouse_id="ph-real"
            )
        )
        db.add(
            LotRow(
                id=REAL_LOT,
                scenario_id="default",
                shipper_id="sh-real",
                shipper_org_id=REAL_SHIPPER_ORG,
                product_id="strawberry",
                pallets=3,
                ready_date=date(2026, 11, 16),
                destination="HKHKG",
                sell_by_date=date(2026, 11, 23),
                source="등록",
                status="planned",
                created_at=STAMP,
                updated_at=STAMP,
            )
        )
        db.add(
            PlanRow(
                plan_id=REAL_PLAN,
                org_id="platform-real",
                scenario_id="default",
                version=1,
                status="planned",
                lot_count=1,
                totals={},
                created_at=STAMP,
                updated_at=STAMP,
                payload={"registered_lot_ids": [REAL_LOT]},
                committed={},
            )
        )
        for device_id in REAL_DEVICES:
            db.add(
                DeviceRow(device_id=device_id, org_id="platform-real", token_hash=f"h-{device_id}")
            )
        db.add(
            LotRow(
                id=DEMO_LOT,
                scenario_id="default",
                shipper_id="sh-jinju",
                shipper_org_id=DEMO_ORG,
                product_id="strawberry",
                pallets=2,
                ready_date=date(2026, 11, 16),
                destination="HKHKG",
                sell_by_date=date(2026, 11, 23),
                source="등록",
                status="registered",
                created_at=STAMP,
                updated_at=STAMP,
            )
        )
        db.flush()
        _coldchain(db, "real", "platform-real", REAL_PLAN, "real-sensor-1")
        db.add(_container("demo-org-c", DEMO_ORG, None))
    quality_history.add(
        ObservationIn(product_id="strawberry", pallets=2, actual_loss_pct=4.0), scope="live"
    )
    quality_history.add(
        ObservationIn(product_id="strawberry", pallets=2, actual_loss_pct=5.0), scope="demo"
    )


def _real_snapshot() -> dict:
    with database.begin() as db:
        return {
            "lot": db.get(LotRow, REAL_LOT).status,
            "plan": db.get(PlanRow, REAL_PLAN).version,
            "devices": sorted(
                db.scalars(select(DeviceRow.device_id).where(DeviceRow.org_id == "platform-real"))
            ),
            "containers": db.scalar(
                select(func.count())
                .select_from(ContainerRow)
                .where(ContainerRow.org_id == "platform-real")
            ),
            "readings": db.scalar(
                select(func.count())
                .select_from(ReadingRow)
                .where(ReadingRow.record_key == "real-c")
            ),
            "alerts": db.scalar(
                select(func.count()).select_from(AlertRow).where(AlertRow.org_id == "platform-real")
            ),
            "bindings": db.scalar(
                select(func.count())
                .select_from(BindingRow)
                .where(BindingRow.org_id == "platform-real")
            ),
            "runs": db.scalar(
                select(func.count()).select_from(RunRow).where(RunRow.org_id == "platform-real")
            ),
            "live_obs": db.scalar(
                select(func.count())
                .select_from(ObservationRow)
                .where(ObservationRow.scope == "live")
            ),
            "demo_plans": db.scalar(
                select(func.count()).select_from(PlanRow).where(PlanRow.org_id == DEMO_ORG)
            ),
            "demo_lot": db.get(LotRow, DEMO_LOT).status,
            "demo_containers": db.scalar(
                select(func.count())
                .select_from(ContainerRow)
                .where(ContainerRow.org_id == DEMO_ORG)
            ),
            "demo_obs": sorted(
                db.scalars(select(ObservationRow.id).where(ObservationRow.scope == "demo"))
            ),
        }


def _plan_children(db, plan_id: str) -> None:
    db.add(PendingRow(replan_id=f"{plan_id}-r", plan_id=plan_id, base_version=1, payload={}))
    db.add(
        OrderRow(
            order_id=f"{plan_id}-o",
            plan_id=plan_id,
            status="requested",
            created_at=STAMP,
            updated_at=STAMP,
            payload={},
        )
    )
    db.add(
        IdempotencyRow(
            plan_id=plan_id, key=f"{plan_id}-k", kind="commit", fingerprint="f", payload={}
        )
    )


def _count(db, row, real_plans) -> int:
    return db.scalar(select(func.count()).select_from(row).where(~row.plan_id.in_(real_plans)))


def _sandbox_activity(platform: TestClient, shipper: TestClient) -> str:
    created = platform.post("/api/saas/platform/plans", json={"include_registered": True})
    assert created.status_code == 201, created.text
    plan_id = created.json()["plan_id"]
    assigned = platform.put(
        f"/api/saas/platform/plans/{plan_id}/forwarder",
        json={"forwarder_org_id": FORWARDER_ORG_ID},
    )
    assert assigned.status_code == 200, assigned.text
    device = platform.post("/api/saas/devices", json={"device_id": "demo-sensor-9"})
    assert device.status_code == 201, device.text
    lot = shipper.post(
        "/api/intake/lots",
        json={
            "product": "strawberry",
            "pallets": 2,
            "ready_date": "2026-11-16",
            "destination": "HKHKG",
            "sell_by_date": "2026-11-23",
            "shipper": "sh-jinju",
            "save": True,
        },
    )
    assert lot.status_code == 200, lot.text
    with database.begin() as db:
        _plan_children(db, plan_id)
        _coldchain(db, "sbx", PLATFORM_ORG_ID, plan_id, "demo-sensor-9")
        db.add(
            ObservationRow(
                id="sbx-manual",
                scope="sandbox",
                origin="manual",
                product_id="strawberry",
                recorded_at=STAMP,
                payload={},
            )
        )
    return plan_id


def _sandbox_snapshot() -> dict:
    with database.begin() as db:
        sandbox_orgs = select(OrganizationRow.id).where(OrganizationRow.sandbox.is_(True))
        real_plans = select(PlanRow.plan_id).where(~PlanRow.org_id.in_(sandbox_orgs))
        return {
            "orgs": sorted(db.scalars(sandbox_orgs)),
            "lots": sorted(
                db.execute(
                    select(
                        LotRow.id,
                        LotRow.shipper_org_id,
                        LotRow.status,
                        LotRow.pallets,
                        LotRow.scenario_id,
                    ).where(LotRow.shipper_org_id.in_(sandbox_orgs))
                ).all()
            ),
            "plans": db.scalar(
                select(func.count()).select_from(PlanRow).where(PlanRow.org_id.in_(sandbox_orgs))
            ),
            "pending": _count(db, PendingRow, real_plans),
            "orders": _count(db, OrderRow, real_plans),
            "idempotency": _count(db, IdempotencyRow, real_plans),
            "forwarders": db.scalar(
                select(func.count())
                .select_from(PlanForwarderRow)
                .where(PlanForwarderRow.forwarder_org_id.in_(sandbox_orgs))
            ),
            "devices": sorted(
                db.scalars(select(DeviceRow.device_id).where(DeviceRow.org_id.in_(sandbox_orgs)))
            ),
            "containers": db.scalar(
                select(func.count())
                .select_from(ContainerRow)
                .where(ContainerRow.org_id.in_(sandbox_orgs))
            ),
            "readings": db.scalar(
                select(func.count()).select_from(ReadingRow).where(ReadingRow.record_key == "sbx-c")
            ),
            "alerts": db.scalar(
                select(func.count()).select_from(AlertRow).where(AlertRow.org_id.in_(sandbox_orgs))
            ),
            "bindings": db.scalar(
                select(func.count())
                .select_from(BindingRow)
                .where(BindingRow.org_id.in_(sandbox_orgs))
            ),
            "runs": db.scalar(
                select(func.count()).select_from(RunRow).where(RunRow.org_id.in_(sandbox_orgs))
            ),
            "observations": sorted(
                db.scalars(select(ObservationRow.id).where(ObservationRow.scope == "sandbox"))
            ),
        }


def test_admin_reset_restores_baseline_and_keeps_real_data(
    saas_app, client, reset_admin_headers, reset_clock
) -> None:
    run_lots()
    baseline_lots = _sandbox_snapshot()["lots"]
    assert len(baseline_lots) == 6
    _real_world()
    assert client.post("/api/plans", json={"scenario_id": "default"}).status_code == 201
    real_client = password_client(saas_app, REAL_PLATFORM_EMAIL, REAL_PLATFORM_PASSWORD)
    platform = login_client(PLATFORM_ORG_ID, saas_app)
    shipper = login_client(SHIPPER_ORG_ID, saas_app)
    _sandbox_activity(platform, shipper)
    real_before = _real_snapshot()
    dirty = _sandbox_snapshot()
    assert dirty["plans"] == 1 and dirty["forwarders"] == 1
    assert dirty["pending"] == dirty["orders"] == dirty["idempotency"] == 1
    assert dirty["devices"] == ["demo-sensor-9"]
    assert len(dirty["lots"]) == 7
    assert "sbx-manual" in dirty["observations"]

    response = TestClient(saas_app, base_url="https://testserver").post(
        "/api/demo/reset", params={"scope": "sandbox"}, headers=reset_admin_headers
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"scope", "orgs", "reset_at"}
    assert body["scope"] == "sandbox"
    assert body["orgs"] == 8
    assert datetime.fromisoformat(body["reset_at"]) == BASE_NOW

    after = _sandbox_snapshot()
    assert after["lots"] == baseline_lots
    assert all(status == "registered" for _, _, status, _, _ in after["lots"])
    wiped = ("plans", "forwarders", "pending", "orders", "idempotency", "containers")
    for key in (*wiped, "readings", "alerts", "bindings", "runs"):
        assert after[key] == 0, key
    assert after["devices"] == []
    assert "sbx-manual" not in after["observations"]
    assert after["observations"] == sorted(f"sandbox-{item.id}" for item in quality_history._seed)
    assert _real_snapshot() == real_before
    assert real_client.get("/api/me").status_code == 200
    assert platform.get("/api/me").status_code == 401
    assert shipper.get("/api/me").status_code == 401

    reset_clock[0] = BASE_NOW + timedelta(seconds=5)
    again = client.post("/api/demo/reset", params={"scope": "sandbox"}, headers=reset_admin_headers)
    assert again.status_code == 200
    assert _sandbox_snapshot() == after
    assert _real_snapshot() == real_before


def test_session_reset_keeps_caller_and_logs_out_other_demo_users(saas_app, reset_clock) -> None:
    run_lots()
    platform = login_client(PLATFORM_ORG_ID, saas_app)
    other_platform = login_client(PLATFORM_ORG_ID, saas_app)
    shipper = login_client(SHIPPER_ORG_ID, saas_app)
    _sandbox_activity(platform, shipper)

    response = platform.post("/api/saas/sandbox/reset")
    assert response.status_code == 200, response.text
    assert response.json()["scope"] == "sandbox"
    assert response.json()["orgs"] == 8
    assert platform.get("/api/me").status_code == 200
    assert other_platform.get("/api/me").status_code == 401
    assert shipper.get("/api/me").status_code == 401
    with database.begin() as db:
        assert db.scalar(select(func.count()).select_from(AuthSessionRow)) == 1
    pool = platform.get("/api/saas/platform/lots")
    assert pool.status_code == 200
    assert len(pool.json()) == 6
    assert platform.get("/api/saas/platform/plans").json() == []


def test_session_reset_cooldown(saas_app, reset_clock) -> None:
    platform = login_client(PLATFORM_ORG_ID, saas_app)
    assert platform.post("/api/saas/sandbox/reset").status_code == 200

    reset_clock[0] = BASE_NOW + timedelta(seconds=20)
    blocked = platform.post("/api/saas/sandbox/reset")
    assert blocked.status_code == 429
    assert blocked.headers["retry-after"] == "40"

    reset_clock[0] = BASE_NOW + timedelta(seconds=60)
    assert platform.post("/api/saas/sandbox/reset").status_code == 200


def test_admin_reset_ignores_cooldown_but_starts_it(
    saas_app, client, reset_admin_headers, reset_clock
) -> None:
    platform = login_client(PLATFORM_ORG_ID, saas_app)
    params = {"scope": "sandbox"}
    for _ in range(2):
        response = client.post("/api/demo/reset", params=params, headers=reset_admin_headers)
        assert response.status_code == 200
    assert platform.post("/api/saas/sandbox/reset").status_code == 401
    platform = login_client(PLATFORM_ORG_ID, saas_app)
    assert platform.post("/api/saas/sandbox/reset").status_code == 429


def test_session_reset_requires_sandbox_platform(saas_app, reset_clock) -> None:
    create_real_platform()
    real = password_client(saas_app, REAL_PLATFORM_EMAIL, REAL_PLATFORM_PASSWORD)
    assert real.post("/api/saas/sandbox/reset").status_code == 403
    assert login_client(SHIPPER_ORG_ID, saas_app).post("/api/saas/sandbox/reset").status_code == 403
    assert (
        login_client(FORWARDER_ORG_ID, saas_app).post("/api/saas/sandbox/reset").status_code == 403
    )
    anonymous = TestClient(saas_app, base_url="https://testserver", headers=SAAS_CSRF)
    assert anonymous.post("/api/saas/sandbox/reset").status_code == 401
    with database.begin() as db:
        assert db.scalar(select(func.count()).select_from(LotRow)) == 0


def test_admin_sandbox_reset_needs_token(saas_app, client, reset_admin_headers) -> None:
    saas = TestClient(saas_app, base_url="https://testserver")
    assert saas.post("/api/demo/reset", params={"scope": "sandbox"}).status_code == 401
    assert (
        saas.post(
            "/api/demo/reset", params={"scope": "sandbox"}, headers={"Authorization": "Bearer x"}
        ).status_code
        == 401
    )
    assert (
        client.post("/api/demo/reset", params={"scope": "world"}, headers=reset_admin_headers)
    ).status_code == 422


def test_demo_scope_reset_unchanged(client, reset_admin_headers, demo_tenants) -> None:
    run_lots()
    assert client.post("/api/plans", json={"scenario_id": "default"}).status_code == 201
    body = client.post("/api/demo/reset", headers=reset_admin_headers).json()
    assert set(body) == {
        "cleared_sessions",
        "cleared_coldchain_containers",
        "cleared_quality_observations",
        "cleared_lots",
    }
    assert body["cleared_sessions"] == 1
    with database.begin() as db:
        assert db.scalar(select(func.count()).select_from(LotRow)) == 6


def test_reset_cli(demo_tenants, capsys, reset_clock) -> None:
    main(["reset-sandbox"])
    printed = capsys.readouterr().out.split()
    assert printed[:2] == ["scope=sandbox", "orgs=8"]
    assert printed[2] == f"reset_at={BASE_NOW.isoformat()}"
    main(["reset-sandbox"])
    capsys.readouterr()
    with database.begin() as db:
        assert db.scalar(select(func.count()).select_from(LotRow)) == 6


def test_reset_seeds_fresh_database(reset_clock) -> None:
    result = sandbox_reset_module.reset_sandbox()
    assert result.orgs == 8
    assert result.reset_at == BASE_NOW
    with database.begin() as db:
        assert db.scalar(select(func.count()).select_from(LotRow)) == 6


def _late_plan(plan_id: str, lot_ids: list[str]) -> None:
    with database.begin() as db:
        db.add(
            PlanRow(
                plan_id=plan_id,
                org_id=PLATFORM_ORG_ID,
                scenario_id="default",
                version=1,
                status="planned",
                lot_count=len(lot_ids),
                totals={},
                created_at=STAMP,
                updated_at=STAMP,
                payload={"registered_lot_ids": lot_ids},
                committed={},
            )
        )


def test_reset_logs_out_first_and_drops_plans_written_mid_reset(
    saas_app, monkeypatch, reset_clock
) -> None:
    run_lots()
    platform = login_client(PLATFORM_ORG_ID, saas_app)
    other = login_client(PLATFORM_ORG_ID, saas_app)
    lot_ids = [lot_id for lot_id, *_ in _sandbox_snapshot()["lots"]]
    seen: list[int] = []
    real_clear = sandbox_reset_module.coldchain_store.clear
    real_baseline = sandbox_reset_module.reset_to_baseline

    def clear(org_id: str) -> int:
        if not seen:
            seen.append(other.get("/api/me").status_code)
            _late_plan("plan-mid-early", lot_ids)
        return real_clear(org_id)

    def baseline(db, *args, **kwargs) -> int:
        _late_plan("plan-mid-late", lot_ids)
        return real_baseline(db, *args, **kwargs)

    monkeypatch.setattr(sandbox_reset_module.coldchain_store, "clear", clear)
    monkeypatch.setattr(sandbox_reset_module, "reset_to_baseline", baseline)
    assert platform.post("/api/saas/sandbox/reset").status_code == 200
    assert seen == [401]
    after = _sandbox_snapshot()
    assert after["plans"] == 0
    assert len(after["lots"]) == 6
    assert all(status == "registered" for _, _, status, _, _ in after["lots"])
    created = platform.post("/api/saas/platform/plans", json={"include_registered": True})
    assert created.status_code == 201, created.text
    with database.begin() as db:
        assert db.get(PlanRow, created.json()["plan_id"]).lot_count == 6


def test_failed_reset_does_not_start_cooldown(saas_app, monkeypatch, reset_clock) -> None:
    platform = login_client(PLATFORM_ORG_ID, saas_app)

    def broken(*args, **kwargs) -> int:
        raise RuntimeError("seed failed")

    monkeypatch.setattr(sandbox_reset_module, "reset_to_baseline", broken)
    with pytest.raises(RuntimeError):
        platform.post("/api/saas/sandbox/reset")
    monkeypatch.undo()
    monkeypatch.setattr(sandbox_reset_module, "clock", lambda: BASE_NOW)
    retried = platform.post("/api/saas/sandbox/reset")
    assert retried.status_code == 200, retried.text
    assert platform.post("/api/saas/sandbox/reset").status_code == 429


def test_reset_with_fuel_shock_scenario(demo_tenants, reset_clock) -> None:
    run_lots()
    main(["reset-sandbox", "--scenario", "fuel-shock"])
    lots = _sandbox_snapshot()["lots"]
    assert len(lots) == 8
    assert {scenario for *_, scenario in lots} == {"fuel-shock"}
