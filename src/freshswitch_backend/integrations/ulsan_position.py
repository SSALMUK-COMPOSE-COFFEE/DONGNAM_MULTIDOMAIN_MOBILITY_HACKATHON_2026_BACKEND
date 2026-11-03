import asyncio
import json
import sys
import xml.etree.ElementTree as ET
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Self

import httpx
from pydantic import BaseModel

from freshswitch_backend.config import get_settings
from freshswitch_backend.integrations.errors import public_error
from freshswitch_backend.integrations.mof_snapshot import SNAPSHOT_ROOT
from freshswitch_backend.integrations.mof_vessel import ERROR_MESSAGES, parse_kst_datetime

BASE_URL = "https://apis.data.go.kr/B551938/VslPstnInfoService"
INFO_PATH = "/getVslPstnInfo"
DATASET_URL = "https://www.data.go.kr/data/15156631/openapi.do"
DATASET_NAME = "울산항만공사_항내 선박위치정보"
MAX_ROWS_PER_PAGE = 1000
MAX_PAGES = 20
LIVE_TIMEOUT_SECONDS = 8.0
LIVE_REQUEST_TIMEOUT_SECONDS = 4.0
ULSAN_SNAPSHOT_PATH = SNAPSHOT_ROOT / "ulsan" / "positions.json"

FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "call_sign": ("callsgn", "clsgn", "callsign", "call_sign", "호출부호"),
    "entry_year": ("ptentyr", "etryptyear", "entryyear", "입항연도"),
    "voyage": ("vyg", "etryptco", "voyage", "항차"),
    "vessel_name": ("vslnm", "vsslnm", "shipnm", "vesselname", "선박명"),
    "lat": ("lat", "la", "latitude", "위도"),
    "lon": ("lon", "lot", "lo", "lng", "longitude", "경도"),
    "sog_knots": ("sog", "spd", "grndspd", "speed", "대지속력"),
    "cog_deg": ("cog", "grndcrs", "course", "대지침로"),
    "rot": ("rot", "trnrt", "선회율"),
    "heading_deg": ("hdgang", "heading", "선수방위"),
    "mmsi": ("mmsino", "mmsi"),
    "imo": ("imono", "imo"),
    "draft_m": ("drft", "draft", "vsldrft", "선박흘수"),
    "nav_status": ("nvgtstts", "navstts", "oprsttus", "navstatus", "sttus", "status", "운항상태"),
    "observed_at": ("updttm", "rcvdt", "regdt", "crtdt", "rcvdttm", "dt", "수신일시"),
}
NUMERIC = {"lat", "lon", "sog_knots", "cog_deg", "rot", "heading_deg", "draft_m"}

PositionSource = Literal["live", "snapshot"]


class UlsanApiError(Exception):
    def __init__(self, code: str, detail: str | None = None) -> None:
        self.code = code
        self.detail = detail
        self.message = ERROR_MESSAGES.get(code, "알 수 없는 에러")
        suffix = f" ({detail})" if detail else ""
        super().__init__(f"울산항 선박위치 API 오류 [{code}] {self.message}{suffix}")


class MissingUlsanKey(Exception):
    def __init__(self) -> None:
        super().__init__("공공데이터포털 키(DATA_GO_KR_SERVICE_KEY)가 설정되지 않았습니다")


class PositionsUnavailable(Exception):
    pass


class VesselPosition(BaseModel):
    call_sign: str | None = None
    entry_year: str | None = None
    voyage: str | None = None
    vessel_name: str | None = None
    mmsi: str | None = None
    imo: str | None = None
    lat: float | None = None
    lon: float | None = None
    sog_knots: float | None = None
    cog_deg: float | None = None
    rot: float | None = None
    heading_deg: float | None = None
    draft_m: float | None = None
    nav_status: str | None = None
    observed_at: datetime | None = None


class PositionSnapshot(BaseModel):
    dataset: str = DATASET_NAME
    fetched_at: datetime
    derived_from: str
    count: int
    positions: list[VesselPosition]


class PositionFeed(BaseModel):
    dataset: str = DATASET_NAME
    dataset_url: str = DATASET_URL
    source: PositionSource
    fetched_at: datetime
    offline_mode: bool
    fallback_reason: str | None = None
    snapshot_note: str | None = None
    count: int
    positions: list[VesselPosition]


def _normalize(key: str) -> str:
    return key.replace("_", "").replace("-", "").lower()


_LOOKUP = {
    _normalize(alias): field for field, aliases in FIELD_ALIASES.items() for alias in aliases
}


def _number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _datetime(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if text.isdigit() and len(text) in (12, 14):
        text = (
            f"{text[:4]}-{text[4:6]}-{text[6:8]} {text[8:10]}:{text[10:12]}:{text[12:14] or '00'}"
        )
    try:
        return parse_kst_datetime(text)
    except ValueError:
        return None


def position_from_item(item: dict[str, Any]) -> VesselPosition | None:
    values: dict[str, Any] = {}
    for key, raw in item.items():
        field = _LOOKUP.get(_normalize(str(key)))
        if field is None or field in values:
            continue
        if field in NUMERIC:
            values[field] = _number(raw)
        elif field == "observed_at":
            values[field] = _datetime(raw)
        else:
            text = str(raw).strip() if raw is not None else ""
            values[field] = text or None
    if not values.get("call_sign") and not values.get("mmsi"):
        return None
    return VesselPosition(**values)


def _items_from_json(data: dict[str, Any]) -> tuple[list[dict[str, Any]], int | None]:
    if "OpenAPI_ServiceResponse" in data:
        header = data["OpenAPI_ServiceResponse"].get("cmmMsgHeader", {})
        raise UlsanApiError(
            str(header.get("returnReasonCode") or "99"), header.get("returnAuthMsg")
        )
    root = data.get("response", data)
    header = root.get("header") or {}
    code = header.get("resultCode")
    if code is not None and str(code) not in ("00", "0"):
        raise UlsanApiError(str(code), header.get("resultMsg"))
    body = root.get("body") or {}
    items = body.get("items") or []
    if isinstance(items, dict):
        items = items.get("item") or []
    if isinstance(items, dict):
        items = [items]
    total = body.get("totalCount")
    return [i for i in items if isinstance(i, dict)], int(total) if total not in (
        None,
        "",
    ) else None


def _items_from_xml(payload: bytes) -> tuple[list[dict[str, Any]], int | None]:
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise UlsanApiError("99", "응답 파싱 실패") from exc
    if root.tag == "OpenAPI_ServiceResponse":
        raise UlsanApiError(
            root.findtext("cmmMsgHeader/returnReasonCode") or "99",
            root.findtext("cmmMsgHeader/returnAuthMsg"),
        )
    code = root.findtext("header/resultCode")
    if code is not None and code not in ("00", "0"):
        raise UlsanApiError(code, root.findtext("header/resultMsg"))
    items = [{child.tag: child.text for child in item} for item in root.iter("item")]
    total = root.findtext("body/totalCount")
    return items, int(total) if total else None


def parse_positions(payload: str | bytes) -> tuple[list[VesselPosition], int]:
    raw = payload.encode() if isinstance(payload, str) else payload
    text = raw.lstrip()
    if text.startswith(b"{"):
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise UlsanApiError("99", "응답 파싱 실패") from exc
        items, total = _items_from_json(data)
    else:
        items, total = _items_from_xml(text)
    positions = [p for item in items if (p := position_from_item(item)) is not None]
    return positions, total if total is not None else len(items)


class UlsanPositionClient:
    def __init__(
        self,
        service_key: str,
        base_url: str = BASE_URL,
        timeout: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._service_key = service_key
        self._client = httpx.AsyncClient(base_url=base_url, timeout=timeout, transport=transport)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self._client.aclose()

    async def fetch_positions(
        self,
        call_sign: str | None = None,
        entry_year: str | None = None,
        voyage: str | None = None,
    ) -> list[VesselPosition]:
        params: dict[str, str | int] = {
            "serviceKey": self._service_key,
            "numOfRows": MAX_ROWS_PER_PAGE,
            "resultType": "json",
        }
        for name, value in (("callsgn", call_sign), ("ptentYr", entry_year), ("vyg", voyage)):
            if value:
                params[name] = value
        positions: list[VesselPosition] = []
        for page in range(1, MAX_PAGES + 1):
            response = await self._client.get(INFO_PATH, params={**params, "pageNo": page})
            head = response.content.lstrip()[:1]
            if response.is_error and head not in (b"{", b"<"):
                response.raise_for_status()
            items, total = parse_positions(response.content)
            positions.extend(items)
            if not items or len(positions) >= total:
                break
        return positions


def load_snapshot(path: Path = ULSAN_SNAPSHOT_PATH) -> PositionSnapshot | None:
    if not path.is_file():
        return None
    return PositionSnapshot.model_validate_json(path.read_text(encoding="utf-8"))


def save_snapshot(snapshot: PositionSnapshot, path: Path = ULSAN_SNAPSHOT_PATH) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(snapshot.model_dump_json(indent=1, exclude_none=True) + "\n", encoding="utf-8")
    return path


LIVE_ERRORS = (MissingUlsanKey, UlsanApiError, httpx.HTTPError, TimeoutError, OSError, ValueError)


def _reason(exc: BaseException) -> str:
    if isinstance(exc, MissingUlsanKey):
        return "울산항 선박위치 API 키가 설정되지 않았습니다"
    return public_error(exc, "울산항 선박위치 API")


class PositionProvider:
    def __init__(
        self,
        client_factory: Callable[[], UlsanPositionClient] | None,
        offline_mode: bool = False,
        snapshot_path: Path = ULSAN_SNAPSHOT_PATH,
        live_timeout: float = LIVE_TIMEOUT_SECONDS,
    ) -> None:
        self._factory = client_factory
        self._offline = offline_mode
        self._path = snapshot_path
        self._timeout = live_timeout

    async def _live(self, call_sign: str | None) -> list[VesselPosition]:
        if self._factory is None:
            raise MissingUlsanKey()
        async with self._factory() as client:
            return await asyncio.wait_for(client.fetch_positions(call_sign), self._timeout)

    async def get(self, call_sign: str | None = None) -> PositionFeed:
        reason = "오프라인 모드" if self._offline else None
        if reason is None:
            try:
                positions = await self._live(call_sign)
                return PositionFeed(
                    source="live",
                    fetched_at=datetime.now(UTC),
                    offline_mode=False,
                    count=len(positions),
                    positions=positions,
                )
            except LIVE_ERRORS as exc:
                reason = _reason(exc)
        snapshot = load_snapshot(self._path)
        if snapshot is None:
            raise PositionsUnavailable(f"{reason}, 저장된 울산항 스냅샷이 없습니다")
        positions = [p for p in snapshot.positions if call_sign is None or p.call_sign == call_sign]
        return PositionFeed(
            source="snapshot",
            fetched_at=snapshot.fetched_at,
            offline_mode=self._offline,
            fallback_reason=None if self._offline else reason,
            snapshot_note=snapshot.derived_from,
            count=len(positions),
            positions=positions,
        )


def main() -> int:
    key = get_settings().data_go_kr_service_key
    if not key:
        print("DATA_GO_KR_SERVICE_KEY가 비어 있습니다", file=sys.stderr)
        return 1

    async def run() -> list[VesselPosition]:
        async with UlsanPositionClient(key, timeout=30) as client:
            return await client.fetch_positions()

    try:
        positions = asyncio.run(run())
    except UlsanApiError as exc:
        print(f"{exc}\n활용신청: {DATASET_NAME} {DATASET_URL}", file=sys.stderr)
        return 2
    path = save_snapshot(
        PositionSnapshot(
            fetched_at=datetime.now(UTC),
            derived_from=f"{DATASET_NAME} 실시간 응답",
            count=len(positions),
            positions=positions,
        )
    )
    print(f"울산항 선박위치 {len(positions)}척 -> {path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
