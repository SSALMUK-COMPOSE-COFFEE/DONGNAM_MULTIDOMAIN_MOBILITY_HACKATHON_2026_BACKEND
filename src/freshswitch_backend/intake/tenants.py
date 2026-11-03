from dataclasses import dataclass, field

from sqlalchemy import select

from freshswitch_backend.domain.models import Lot, Packhouse, Scenario, Shipper
from freshswitch_backend.lots import ANY_ORG
from freshswitch_backend.persistence import database
from freshswitch_backend.persistence.tables import (
    OrganizationRow,
    PackhouseRow,
    ShipperProfileRow,
)


@dataclass(frozen=True)
class TenantShipper:
    shipper: Shipper
    org_id: str
    org_name: str
    product_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class TenantCatalog:
    shippers: list[TenantShipper] = field(default_factory=list)
    packhouses: list[Packhouse] = field(default_factory=list)

    @property
    def shipper_ids(self) -> list[str]:
        return [s.shipper.id for s in self.shippers]

    def org_of(self, shipper_id: str) -> str | None:
        return next((s.org_id for s in self.shippers if s.shipper.id == shipper_id), None)

    def org_names(self) -> dict[str, str]:
        return {s.shipper.id: s.org_name for s in self.shippers}


def load_catalog(org_id: str, base: Scenario | None = None) -> TenantCatalog:
    query = (
        select(ShipperProfileRow, PackhouseRow, OrganizationRow.name)
        .join(PackhouseRow, PackhouseRow.id == ShipperProfileRow.packhouse_id)
        .join(OrganizationRow, OrganizationRow.id == ShipperProfileRow.org_id)
        .where(OrganizationRow.type == "shipper")
        .order_by(ShipperProfileRow.created_at, ShipperProfileRow.id)
    )
    if org_id != ANY_ORG:
        query = query.where(ShipperProfileRow.org_id == org_id)
    with database.begin() as db:
        rows = db.execute(query).all()
    order = {s.id: i for i, s in enumerate(base.shippers)} if base is not None else {}
    rows = sorted(rows, key=lambda row: order.get(row[0].id, len(order)))
    shippers: list[TenantShipper] = []
    packhouses: dict[str, Packhouse] = {}
    for profile, packhouse, org_name in rows:
        shippers.append(
            TenantShipper(
                shipper=Shipper(
                    id=profile.id,
                    name=profile.name,
                    packhouse_id=profile.packhouse_id,
                    source=profile.source,
                ),
                org_id=profile.org_id,
                org_name=org_name,
                product_ids=tuple(profile.product_ids or ()),
            )
        )
        packhouses.setdefault(
            packhouse.id,
            Packhouse(
                id=packhouse.id,
                name=packhouse.name,
                region=packhouse.region,
                lat=packhouse.lat,
                lon=packhouse.lon,
                source=packhouse.source,
            ),
        )
    return TenantCatalog(shippers=shippers, packhouses=list(packhouses.values()))


def tenant_scenario(
    base: Scenario, catalog: TenantCatalog, lots: list[Lot] | None = None
) -> Scenario:
    return base.model_copy(
        update={
            "shippers": [s.shipper for s in catalog.shippers],
            "packhouses": catalog.packhouses,
            "lots": list(lots or []),
        },
        deep=True,
    )
