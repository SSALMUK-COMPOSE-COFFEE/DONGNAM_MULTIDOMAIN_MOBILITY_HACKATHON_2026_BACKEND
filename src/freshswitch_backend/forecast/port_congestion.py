from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel

from freshswitch_backend.integrations.ulsan_position import (
    DATASET_NAME,
    DATASET_URL,
    PositionFeed,
    PositionSource,
    VesselPosition,
)

CongestionLevel = Literal["normal", "elevated", "high"]
Category = Literal["waiting", "berthed", "underway", "other"]

STATIONARY_SOG_KNOTS = 1.0
STALE_HOURS = 24.0
ANCHORED_STATUS = "정박(앵커링)"
MOORED_STATUS = "정박(계류)"
FISHING_STATUS = "낚시"


class Area(BaseModel):
    name: str
    south: float
    north: float
    west: float
    east: float
    source: str = "가정"

    def contains(self, lat: float, lon: float) -> bool:
        return self.south <= lat <= self.north and self.west <= lon <= self.east


ANCHORAGE_AREAS: list[Area] = [
    Area(
        name="울산항 외곽 묘박지(동쪽 해상)",
        south=35.38,
        north=35.47,
        west=129.405,
        east=129.50,
        source="가정: 2026-10-30 실제 스냅샷에서 '정박(앵커링)' 대형선 22척이 모인 범위를 감싼 사각형",
    ),
]
BERTH_AREAS: list[Area] = [
    Area(
        name="온산항·울산 본항 내항",
        south=35.40,
        north=35.54,
        west=129.33,
        east=129.405,
        source="가정: 같은 스냅샷에서 '정박(계류)' 대형선이 모인 범위",
    ),
    Area(
        name="울산 본항 동측·미포",
        south=35.475,
        north=35.54,
        west=129.405,
        east=129.445,
        source="가정: 같은 스냅샷에서 '정박(계류)' 대형선이 모인 범위",
    ),
]


class Thresholds(BaseModel):
    elevated_ratio: float = 0.30
    high_ratio: float = 0.45
    elevated_waiting: int = 35
    high_waiting: int = 50
    source: str = (
        "가정: 2026-10-30 23시(KST) 실제 스냅샷 한 장(대기 24척·접안 79척, 대기 비율 0.23)을 "
        "평시로 보고 비율 약 1.3배·2배, 대기 척수 약 1.5배·2배를 경계로 잡음. 관측이 쌓이면 분위수로 바꾼다"
    )


THRESHOLDS = Thresholds()

RULES = [
    "중복 제거: IMO → MMSI → 호출부호가 같으면 관측시각이 가장 늦은 것 하나만 쓴다",
    f"오래된 위치 제외: 피드의 가장 늦은 관측시각보다 {STALE_HOURS:g}시간 넘게 지난 위치(가정)",
    (
        f"소형선 제외: IMO 번호 없음, 흘수 0 또는 없음, 운항상태 '{FISHING_STATUS}' "
        "(예선·어선·급유선 등 국내 소형선은 IMO가 없고 흘수를 보내지 않는다)"
    ),
    (
        f"정지 판정: 대지속력 {STATIONARY_SOG_KNOTS:g}노트 이하(가정, AIS 속력 잡음). "
        "실제 스냅샷에서 '항해(동력)'으로 신고된 대형선 50척 중 다수가 속력 0이라 운항상태만 믿지 않는다"
    ),
    f"대기: 정지 + (운항상태 '{ANCHORED_STATUS}' 또는 묘박지 범위 안)",
    f"접안: 정지 + (운항상태 '{MOORED_STATUS}' 또는 부두 범위 안)",
    "항행: 대지속력이 정지 기준보다 큼",
    "기타: 정지했지만 묘박지·부두 밖(외해 표류, 다른 항만 부근). 비율 계산에서 뺀다",
    "대기 비율 = 대기 / (대기 + 접안)",
]


class ClassifiedVessel(BaseModel):
    vessel_name: str | None
    call_sign: str | None
    mmsi: str | None
    imo: str | None
    lat: float
    lon: float
    sog_knots: float | None
    draft_m: float | None
    nav_status: str | None
    observed_at: datetime | None
    category: Category
    area: str | None


class CongestionCounts(BaseModel):
    total: int
    duplicates: int
    stale: int
    small_craft: int
    no_position: int
    considered: int
    waiting: int
    berthed: int
    underway: int
    other: int


class PortCongestionSummary(BaseModel):
    port: str = "울산항"
    dataset: str = DATASET_NAME
    dataset_url: str = DATASET_URL
    source: PositionSource
    fetched_at: datetime
    observed_until: datetime | None
    offline_mode: bool
    fallback_reason: str | None = None
    counts: CongestionCounts
    waiting_ratio: float
    level: CongestionLevel
    thresholds: Thresholds = THRESHOLDS
    anchorage_areas: list[Area] = ANCHORAGE_AREAS
    berth_areas: list[Area] = BERTH_AREAS
    rules: list[str] = RULES
    basis: str


class PortCongestion(PortCongestionSummary):
    vessels: list[ClassifiedVessel]

    def summary(self) -> PortCongestionSummary:
        return PortCongestionSummary(**self.model_dump(exclude={"vessels"}))


def _key(p: VesselPosition) -> str | None:
    return p.imo or p.mmsi or p.call_sign


def _newer(a: VesselPosition, b: VesselPosition) -> bool:
    if a.observed_at is None:
        return False
    return b.observed_at is None or a.observed_at > b.observed_at


def _area(areas: Iterable[Area], lat: float, lon: float) -> str | None:
    return next((a.name for a in areas if a.contains(lat, lon)), None)


def categorize(p: VesselPosition) -> tuple[Category, str | None]:
    assert p.lat is not None and p.lon is not None
    anchorage = _area(ANCHORAGE_AREAS, p.lat, p.lon)
    berth = _area(BERTH_AREAS, p.lat, p.lon)
    stationary = (
        p.sog_knots <= STATIONARY_SOG_KNOTS
        if p.sog_knots is not None
        else (p.nav_status or "").startswith("정박")
    )
    if not stationary:
        return "underway", anchorage or berth
    if p.nav_status == ANCHORED_STATUS or anchorage:
        return "waiting", anchorage or berth
    if p.nav_status == MOORED_STATUS or berth:
        return "berthed", berth or anchorage
    return "other", None


def is_small_craft(p: VesselPosition) -> bool:
    return not p.imo or not (p.draft_m or 0) > 0 or p.nav_status == FISHING_STATUS


def level_of(waiting: int, ratio: float, thresholds: Thresholds = THRESHOLDS) -> CongestionLevel:
    if ratio >= thresholds.high_ratio or waiting >= thresholds.high_waiting:
        return "high"
    if ratio >= thresholds.elevated_ratio or waiting >= thresholds.elevated_waiting:
        return "elevated"
    return "normal"


LEVEL_LABELS: dict[CongestionLevel, str] = {
    "normal": "평시",
    "elevated": "혼잡",
    "high": "매우 혼잡",
}


def build_indicator(feed: PositionFeed) -> PortCongestion:
    latest: dict[str, VesselPosition] = {}
    unkeyed: list[VesselPosition] = []
    for p in feed.positions:
        key = _key(p)
        if key is None:
            unkeyed.append(p)
            continue
        current = latest.get(key)
        if current is None or _newer(p, current):
            latest[key] = p
    unique = [*latest.values(), *unkeyed]
    times = [p.observed_at for p in unique if p.observed_at is not None]
    until = max(times, default=None)
    cutoff = until - timedelta(hours=STALE_HOURS) if until else None
    stale = small = no_position = 0
    vessels: list[ClassifiedVessel] = []
    for p in unique:
        if cutoff is not None and (p.observed_at is None or p.observed_at < cutoff):
            stale += 1
            continue
        if is_small_craft(p):
            small += 1
            continue
        if p.lat is None or p.lon is None:
            no_position += 1
            continue
        category, area = categorize(p)
        vessels.append(
            ClassifiedVessel(
                vessel_name=p.vessel_name,
                call_sign=p.call_sign,
                mmsi=p.mmsi,
                imo=p.imo,
                lat=p.lat,
                lon=p.lon,
                sog_knots=p.sog_knots,
                draft_m=p.draft_m,
                nav_status=p.nav_status,
                observed_at=p.observed_at,
                category=category,
                area=area,
            )
        )
    tally = {
        c: sum(1 for v in vessels if v.category == c)
        for c in ("waiting", "berthed", "underway", "other")
    }
    waiting, berthed = tally["waiting"], tally["berthed"]
    ratio = waiting / (waiting + berthed) if waiting + berthed else 0.0
    level = level_of(waiting, ratio)
    origin = "실시간" if feed.source == "live" else "스냅샷"
    basis = (
        f"울산항 {origin} AIS {len(feed.positions)}척 중 대형 상선 {len(vessels)}척: "
        f"묘박 대기 {waiting}척, 접안 {berthed}척, 항행 {tally['underway']}척 → "
        f"대기 비율 {ratio:.2f}, 대기 {waiting}척 → {LEVEL_LABELS[level]}({level}). "
        f"경계(가정) 비율 {THRESHOLDS.elevated_ratio:g}/{THRESHOLDS.high_ratio:g}, "
        f"대기 척수 {THRESHOLDS.elevated_waiting}/{THRESHOLDS.high_waiting}"
    )
    return PortCongestion(
        source=feed.source,
        fetched_at=feed.fetched_at,
        observed_until=until,
        offline_mode=feed.offline_mode,
        fallback_reason=feed.fallback_reason,
        counts=CongestionCounts(
            total=len(feed.positions),
            duplicates=len(feed.positions) - len(unique),
            stale=stale,
            small_craft=small,
            no_position=no_position,
            considered=len(vessels),
            waiting=waiting,
            berthed=berthed,
            underway=tally["underway"],
            other=tally["other"],
        ),
        waiting_ratio=round(ratio, 3),
        level=level,
        basis=basis,
        vessels=vessels,
    )
