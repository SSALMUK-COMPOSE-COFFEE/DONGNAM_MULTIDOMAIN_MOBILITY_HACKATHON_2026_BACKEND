from sqlalchemy import select
from sqlalchemy.orm import Session

from freshswitch_backend.domain.models import Product
from freshswitch_backend.persistence.tables import ProductRow


def product_rows(db: Session, org_id: str) -> list[ProductRow]:
    return list(
        db.scalars(
            select(ProductRow)
            .where(ProductRow.org_id == org_id)
            .order_by(ProductRow.created_at, ProductRow.id)
        )
    )


def active_products(base: list[Product], rows: list[ProductRow]) -> list[Product]:
    replaced = {row.replaces_id for row in rows if row.replaces_id is not None}
    return [p for p in base if p.id not in replaced] + [
        Product.model_validate(row.data) for row in rows if not row.retired
    ]
