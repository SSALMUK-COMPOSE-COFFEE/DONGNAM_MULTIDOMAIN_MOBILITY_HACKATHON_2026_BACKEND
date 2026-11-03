import argparse
import os
from dataclasses import dataclass, field

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from freshswitch_backend.data.loader import get_scenario
from freshswitch_backend.persistence.database import Database, database
from freshswitch_backend.persistence.orgs import insert_demo_org
from freshswitch_backend.persistence.passwords import hash_password
from freshswitch_backend.persistence.tables import (
    MembershipRow,
    OrganizationRow,
    PackhouseRow,
    ShipperProfileRow,
    UserRow,
)

SCENARIO_ID = "default"
DEMO_PASSWORD_ENV = "DEMO_SEED_PASSWORD"
DEFAULT_DEMO_PASSWORD = "freshswitch-demo"
DEMO_EMAIL_DOMAIN = "demo.freshswitch.ssalhub.xyz"

PLATFORM_ORG = ("freshswitch", "프레시스위치")
FORWARDER_ORGS = (
    ("forwarder-dongnam", "동남 콜드체인 포워딩"),
    ("forwarder-sinhang", "신항 해운 포워딩"),
)


@dataclass(frozen=True)
class DemoOrg:
    id: str
    type: str
    name: str
    user_name: str
    shipper_ids: tuple[str, ...] = field(default=())

    @property
    def user_id(self) -> str:
        return f"user-{self.id}"

    @property
    def email(self) -> str:
        return f"{self.id}@{DEMO_EMAIL_DOMAIN}"


def shipper_org_id(shipper_id: str) -> str:
    return "shipper-" + shipper_id.removeprefix("sh-")


def demo_orgs() -> list[DemoOrg]:
    scenario = get_scenario(SCENARIO_ID)
    if scenario is None:
        raise RuntimeError(f"시나리오 {SCENARIO_ID}를 찾을 수 없습니다")
    platform_id, platform_name = PLATFORM_ORG
    orgs = [DemoOrg(platform_id, "platform", platform_name, f"{platform_name} 운영 담당")]
    orgs += [
        DemoOrg(shipper_org_id(s.id), "shipper", s.name, f"{s.name} 담당", (s.id,))
        for s in scenario.shippers
    ]
    orgs += [DemoOrg(oid, "forwarder", name, f"{name} 담당") for oid, name in FORWARDER_ORGS]
    return orgs


def _upsert(db: Session, row: type, values: list[dict]) -> None:
    if values:
        db.execute(insert(row).values(values).on_conflict_do_nothing())


def seed_demo_tenants(db: Session, password: str | None = None) -> None:
    scenario = get_scenario(SCENARIO_ID)
    if scenario is None:
        raise RuntimeError(f"시나리오 {SCENARIO_ID}를 찾을 수 없습니다")
    secret = password or os.environ.get(DEMO_PASSWORD_ENV) or DEFAULT_DEMO_PASSWORD
    orgs = demo_orgs()
    insert_demo_org(db)
    _upsert(db, OrganizationRow, [{"id": o.id, "type": o.type, "name": o.name} for o in orgs])

    existing = db.execute(
        select(UserRow.id, UserRow.email).where(
            or_(
                UserRow.id.in_([o.user_id for o in orgs]),
                UserRow.email.in_([o.email for o in orgs]),
            )
        )
    ).all()
    taken = {value for row in existing for value in row}
    _upsert(
        db,
        UserRow,
        [
            {
                "id": o.user_id,
                "email": o.email,
                "password_hash": hash_password(secret),
                "name": o.user_name,
                "is_demo": True,
            }
            for o in orgs
            if o.user_id not in taken and o.email not in taken
        ],
    )
    demo_users = set(
        db.scalars(
            select(UserRow.id).where(
                UserRow.id.in_([o.user_id for o in orgs]), UserRow.is_demo.is_(True)
            )
        )
    )
    _upsert(
        db,
        MembershipRow,
        [
            {"user_id": o.user_id, "org_id": o.id, "role": "admin"}
            for o in orgs
            if o.user_id in demo_users
        ],
    )

    packhouses = {p.id: p for p in scenario.packhouses}
    products: dict[str, list[str]] = {}
    for lot in scenario.lots:
        bucket = products.setdefault(lot.shipper_id, [])
        if lot.product_id not in bucket:
            bucket.append(lot.product_id)
    shippers = {s.id: s for s in scenario.shippers}
    owned = [(o.id, shippers[sid]) for o in orgs for sid in o.shipper_ids]
    _upsert(
        db,
        PackhouseRow,
        [
            {
                "id": ph.id,
                "org_id": org_id,
                "name": ph.name,
                "region": ph.region,
                "lat": ph.lat,
                "lon": ph.lon,
                "source": ph.source,
            }
            for org_id, shipper in owned
            for ph in [packhouses[shipper.packhouse_id]]
        ],
    )
    _upsert(
        db,
        ShipperProfileRow,
        [
            {
                "id": shipper.id,
                "org_id": org_id,
                "name": shipper.name,
                "packhouse_id": shipper.packhouse_id,
                "product_ids": products.get(shipper.id, []),
                "source": shipper.source,
            }
            for org_id, shipper in owned
        ],
    )


def org_counts(db: Session) -> dict[str, int]:
    rows = db.execute(
        select(OrganizationRow.type, func.count()).group_by(OrganizationRow.type)
    ).all()
    return {kind: count for kind, count in rows}


def run(target: Database | None = None, password: str | None = None) -> dict[str, int]:
    with (target or database).begin() as db:
        seed_demo_tenants(db, password)
        return org_counts(db)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="SaaS 데모 조직·계정 시드")
    parser.add_argument("--password", default=None)
    args = parser.parse_args(argv)
    counts = run(password=args.password)
    print(" ".join(f"{kind}={count}" for kind, count in sorted(counts.items())))


if __name__ == "__main__":
    main()
