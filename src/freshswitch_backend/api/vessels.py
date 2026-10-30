from collections.abc import Callable
from datetime import date, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from freshswitch_backend.config import get_settings
from freshswitch_backend.integrations import (
    MissingServiceKey,
    MofVesselClient,
    MovementFeed,
    MovementProvider,
    MovementsUnavailable,
    VesselMovement,
)
from freshswitch_backend.integrations.mof_provider import LIVE_REQUEST_TIMEOUT_SECONDS
from freshswitch_backend.integrations.mof_vessel import KST

router = APIRouter(prefix="/vessels", tags=["vessels"])

VesselClientFactory = Callable[[], MofVesselClient]


def _default_factory() -> MofVesselClient:
    key = get_settings().data_go_kr_service_key
    if not key:
        raise MissingServiceKey()
    return MofVesselClient(key, timeout=LIVE_REQUEST_TIMEOUT_SECONDS)


def vessel_client_factory() -> VesselClientFactory:
    return _default_factory


def movement_provider(factory: VesselClientFactory) -> MovementProvider:
    return MovementProvider(factory, offline_mode=get_settings().offline_mode)


async def load_busan_feed(
    factory: VesselClientFactory,
    start: date,
    end: date,
    direction: Literal["I", "O"] | None,
    demo: bool = False,
) -> MovementFeed:
    try:
        return await movement_provider(factory).get(start, end, direction, demo=demo)
    except MovementsUnavailable as exc:
        raise HTTPException(
            status_code=503, detail=f"해수부 선박입출항 정보를 가져오지 못했습니다: {exc}"
        ) from exc


async def fetch_busan_movements(
    factory: VesselClientFactory,
    start: date,
    end: date,
    direction: Literal["I", "O"] | None,
    demo: bool = False,
) -> list[VesselMovement]:
    return (await load_busan_feed(factory, start, end, direction, demo=demo)).movements


@router.get("/busan")
async def busan_movements(
    factory: Annotated[VesselClientFactory, Depends(vessel_client_factory)],
    date_str: Annotated[str | None, Query(alias="date", pattern=r"^\d{8}$")] = None,
    direction: Literal["I", "O"] | None = None,
    demo: bool = False,
) -> MovementFeed:
    day = datetime.strptime(date_str, "%Y%m%d").date() if date_str else datetime.now(KST).date()
    return await load_busan_feed(factory, day, day, direction, demo=demo)
