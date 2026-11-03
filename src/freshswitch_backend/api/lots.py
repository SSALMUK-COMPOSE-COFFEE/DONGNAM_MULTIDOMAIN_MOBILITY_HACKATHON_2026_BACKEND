from datetime import date, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import delete, select

from freshswitch_backend.api.auth import OptionalPrincipal, current_org_id
from freshswitch_backend.api.intake import DEMO_SCOPE, IntakeScope, Scope, intake_error, register
from freshswitch_backend.data.loader import get_scenario
from freshswitch_backend.domain.models import Scenario
from freshswitch_backend.intake.models import FieldError, IntakePreview, IntakeRequest
from freshswitch_backend.intake.service import IntakeError, resolve_names
from freshswitch_backend.lots import LotFilter, LotPatch, LotStatus, RegisteredLot, lot_store
from freshswitch_backend.persistence import database
from freshswitch_backend.persistence.tables import LotRow

router = APIRouter(prefix="/lots", tags=["lots"])

OrgId = Annotated[str, Depends(current_org_id)]
LOCKED_STATUSES = frozenset({"planned", "shipped"})
LOT_NOT_FOUND = "등록된 로트를 찾을 수 없습니다"
LOT_LOCKED = (
    "계획에 들어간 출하는 직접 수정·삭제할 수 없습니다. 감량·취소·항공 전환은 "
    "계획 상세에서 변경 요청으로 보내 주세요"
)
LOT_SHIPPED = "이미 선적된 출하는 수정·삭제할 수 없습니다"
LOCKED_STATUS_FORBIDDEN = "화주는 등록·취소 상태로만 바꿀 수 있습니다"


class LotLocked(Exception):
    def __init__(self, status: str) -> None:
        super().__init__(status)
        self.status = status


def _locked_detail(status: str | None) -> str:
    return LOT_SHIPPED if status == "shipped" else LOT_LOCKED


def _lot(lot_id: str, org_id: str) -> RegisteredLot:
    lot = lot_store.get(lot_id, org_id=org_id)
    if lot is None:
        raise HTTPException(status_code=404, detail="등록된 로트를 찾을 수 없습니다")
    return lot


@router.post("", status_code=status.HTTP_201_CREATED)
def create_lot(body: IntakeRequest, scope: Scope) -> IntakePreview:
    return register(body, scope)


@router.get("")
def list_lots(
    org_id: OrgId,
    scenario_id: str | None = None,
    shipper: str | None = None,
    product: str | None = None,
    destination: str | None = None,
    status: LotStatus | None = None,
    ready_from: date | None = None,
    ready_to: date | None = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[RegisteredLot]:
    return lot_store.search(
        LotFilter(
            scenario_id=scenario_id,
            shipper=shipper,
            product=product,
            destination=destination,
            status=status,
            ready_from=ready_from,
            ready_to=ready_to,
            limit=limit,
            offset=offset,
        ),
        org_id=org_id,
    )


@router.get("/{lot_id}")
def read_lot(lot_id: str, org_id: OrgId) -> RegisteredLot:
    return _lot(lot_id, org_id)


def _validate_patch(
    lot: RegisteredLot,
    patch: LotPatch,
    scope: IntakeScope = DEMO_SCOPE,
    recompute_sell_by: bool = False,
) -> dict[str, Any]:
    changes = patch.model_dump(exclude_unset=True)
    values: dict[str, Any] = {}
    errors: list[FieldError] = []
    scenario: Scenario | None = (
        get_scenario(lot.scenario_id) if scope is DEMO_SCOPE else scope.scenario(lot.scenario_id)
    )
    if {"product", "shipper", "destination"} & changes.keys():
        if scenario is None:
            raise HTTPException(
                status_code=422, detail="로트의 시나리오가 없어 이름을 다시 찾을 수 없습니다"
            )
        resolved = resolve_names(
            scenario,
            IntakeRequest(
                product=changes.get("product") or lot.product_id,
                shipper=changes.get("shipper") or lot.shipper_id,
                destination=changes.get("destination") or lot.destination,
                pallets=lot.pallets,
                ready_date=lot.ready_date,
            ),
        )
        values.update(
            product_id=resolved.product_id,
            shipper_id=resolved.shipper_id,
            packhouse=resolved.packhouse_id,
            destination=resolved.destination_port_id,
        )
    for name in ("ready_date", "sell_by_date", "sell_window", "status", "pallets"):
        if changes.get(name) is not None:
            values[name] = changes[name]
    if "pallets" in values:
        capacity = (
            max((c.capacity_pallets for c in scenario.containers), default=20) if scenario else 20
        )
        if values["pallets"] <= 0:
            errors.append(FieldError(field="pallets", message="파렛트 수는 1 이상이어야 합니다"))
        elif values["pallets"] > capacity:
            errors.append(
                FieldError(
                    field="pallets",
                    message=f"컨테이너 1대 용량({capacity}파렛트)을 넘습니다. 나눠서 등록하세요",
                )
            )
    ready = values.get("ready_date", lot.ready_date)
    if (
        recompute_sell_by
        and scenario is not None
        and changes.get("sell_by_date") is None
        and ({"ready_date", "product"} & {k for k, v in changes.items() if v is not None})
    ):
        product_id = values.get("product_id", lot.product_id)
        product = next((p for p in scenario.products if p.id == product_id), None)
        if product is not None:
            values["sell_by_date"] = ready + timedelta(days=product.shelf_life_days)
    sell_by = values.get("sell_by_date", lot.sell_by_date)
    if sell_by < ready:
        errors.append(
            FieldError(field="sell_by_date", message="판매기한이 출하 가능일보다 빠릅니다")
        )
    if errors:
        raise IntakeError(errors)
    return values


def update_locked_lot(
    lot_id: str, patch: LotPatch, scope: IntakeScope, org_id: str
) -> RegisteredLot:
    if patch.status in LOCKED_STATUSES:
        raise HTTPException(status_code=422, detail=LOCKED_STATUS_FORBIDDEN)

    def change(lot: RegisteredLot) -> dict[str, Any]:
        if lot.status in LOCKED_STATUSES:
            raise LotLocked(lot.status)
        return _validate_patch(lot, patch, scope, recompute_sell_by=True)

    try:
        updated = lot_store.update(lot_id, change, org_id=org_id)
    except LotLocked as exc:
        raise HTTPException(status_code=409, detail=_locked_detail(exc.status)) from exc
    except IntakeError as exc:
        raise intake_error(exc) from exc
    if updated is None:
        raise HTTPException(status_code=404, detail=LOT_NOT_FOUND)
    return updated


def delete_unlocked_lot(lot_id: str, org_id: str) -> None:
    with database.begin() as db:
        owned = (LotRow.id == lot_id) & (LotRow.shipper_org_id == org_id)
        removed = db.execute(
            delete(LotRow).where(owned, LotRow.status.not_in(LOCKED_STATUSES))
        ).rowcount
        if removed:
            return
        status = db.scalar(select(LotRow.status).where(owned))
    if status is not None:
        raise HTTPException(status_code=409, detail=_locked_detail(status))
    raise HTTPException(status_code=404, detail=LOT_NOT_FOUND)


@router.patch("/{lot_id}")
def update_lot(lot_id: str, patch: LotPatch, scope: Scope, org_id: OrgId) -> RegisteredLot:
    if scope is not DEMO_SCOPE:
        return update_locked_lot(lot_id, patch, scope, org_id)
    try:
        updated = lot_store.update(lot_id, lambda lot: _validate_patch(lot, patch), org_id=org_id)
    except IntakeError as exc:
        raise intake_error(exc) from exc
    if updated is None:
        raise HTTPException(status_code=404, detail=LOT_NOT_FOUND)
    return updated


@router.delete("/{lot_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_lot(lot_id: str, org_id: OrgId, principal: OptionalPrincipal) -> None:
    if principal is not None:
        delete_unlocked_lot(lot_id, org_id)
        return
    if not lot_store.delete(lot_id, org_id=org_id):
        raise HTTPException(status_code=404, detail=LOT_NOT_FOUND)
