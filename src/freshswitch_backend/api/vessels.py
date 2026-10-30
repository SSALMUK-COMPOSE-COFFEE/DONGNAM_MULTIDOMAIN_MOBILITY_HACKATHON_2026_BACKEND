from collections.abc import Callable
from datetime import date, datetime
from typing import Annotated, Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query

from freshswitch_backend.config import get_settings
from freshswitch_backend.integrations import MofVesselApiError, MofVesselClient, VesselMovement
from freshswitch_backend.integrations.mof_vessel import KST

router = APIRouter(prefix="/vessels", tags=["vessels"])

VesselClientFactory = Callable[[], MofVesselClient]


def _default_factory() -> MofVesselClient:
    key = get_settings().data_go_kr_service_key
    if not key:
        raise HTTPException(
            status_code=503,
            detail="해수부 API 키(DATA_GO_KR_SERVICE_KEY)가 설정되지 않아 선박 입출항 정보를 조회할 수 없습니다",
        )
    return MofVesselClient(key)


def vessel_client_factory() -> VesselClientFactory:
    return _default_factory


async def fetch_busan_movements(
    factory: VesselClientFactory,
    start: date,
    end: date,
    direction: Literal["I", "O"] | None,
) -> list[VesselMovement]:
    client = factory()
    try:
        async with client:
            return await client.fetch_movements(start, end, direction=direction)
    except MofVesselApiError as exc:
        raise HTTPException(status_code=503, detail=f"해수부 선박입출항 API 오류: {exc}") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=503, detail="해수부 선박입출항 API에 연결하지 못했습니다"
        ) from exc


@router.get("/busan")
async def busan_movements(
    factory: Annotated[VesselClientFactory, Depends(vessel_client_factory)],
    date_str: Annotated[str | None, Query(alias="date", pattern=r"^\d{8}$")] = None,
    direction: Literal["I", "O"] | None = None,
) -> list[VesselMovement]:
    day = datetime.strptime(date_str, "%Y%m%d").date() if date_str else datetime.now(KST).date()
    return await fetch_busan_movements(factory, day, day, direction)
