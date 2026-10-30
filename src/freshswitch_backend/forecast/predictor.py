import math
from collections import defaultdict
from collections.abc import Iterable, Sequence
from datetime import date
from functools import lru_cache
from typing import Literal

from pydantic import BaseModel

from freshswitch_backend.forecast.history import (
    DepartureRecord,
    all_records,
    operator_of,
    terminal_of,
    vessel_key,
)

Weather = Literal["normal", "strong_wind", "typhoon"]
Congestion = Literal["normal", "elevated", "high"]
Confidence = Literal["high", "medium", "low"]
Level = Literal[
    "prior", "global", "container", "operator", "terminal", "terminal_weekday", "vessel"
]

PRIOR_P50_HOURS = 1.0
PRIOR_P90_HOURS = 8.0
SHRINK_K = 10.0
MIN_LEVEL_SAMPLES = 3
WEATHER_HOURS: dict[Weather, tuple[float, float]] = {
    "normal": (0.0, 0.0),
    "strong_wind": (6.0, 12.0),
    "typhoon": (36.0, 72.0),
}
CONGESTION_FACTOR: dict[Congestion, float] = {"normal": 1.0, "elevated": 1.25, "high": 1.5}
WEEKDAYS = "월화수목금토일"
LEVEL_LABELS: dict[Level, str] = {
    "prior": "기본 사전값",
    "global": "부산항 전체",
    "container": "컨테이너선",
    "operator": "선사",
    "terminal": "터미널",
    "terminal_weekday": "터미널·요일",
    "vessel": "선박",
}
METHOD = (
    "해수부 출항 신고의 출항예정일시 대비 실제 출항일시 차이(시간)를 표본으로, "
    "부산항 전체 → 컨테이너선 → 선사 → 터미널 → 터미널·요일 → 선박 순으로 "
    "분위수(p50·p90)를 표본 수 가중 n/(n+10)로 섞는다. 표본이 3건 미만인 단계는 건너뛴다. "
    "그 뒤 혼잡 배율과 기상 가산(가정)을 적용한다."
)


class Assumption(BaseModel):
    name: str
    value: float
    unit: str
    source: str = "가정"


class LevelStat(BaseModel):
    level: Level
    label: str
    key: str | None
    sample_size: int
    p50_hours: float
    p90_hours: float
    weight: float


class DelayQuery(BaseModel):
    vessel_name: str | None = None
    call_sign: str | None = None
    terminal: str | None = None
    planned_departure: date | None = None
    container: bool = True


class DelayForecast(BaseModel):
    p50_hours: float
    p90_hours: float
    base_p50_hours: float
    base_p90_hours: float
    confidence: Confidence
    sample_size: int
    basis: list[LevelStat]
    assumptions: list[Assumption]
    weather: Weather
    congestion: Congestion
    congestion_factor: float
    congestion_added_p50_hours: float
    congestion_added_p90_hours: float
    method: str = METHOD
    summary: str


def quantile(values: Sequence[float], q: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("빈 표본")
    pos = (len(ordered) - 1) * q
    low, high = math.floor(pos), math.ceil(pos)
    return ordered[low] + (ordered[high] - ordered[low]) * (pos - low)


def _assumptions(weather: Weather, congestion: Congestion) -> list[Assumption]:
    add50, add90 = WEATHER_HOURS[weather]
    items = [
        Assumption(
            name="기본 사전값 p50·p90 (표본이 전혀 없을 때)",
            value=PRIOR_P50_HOURS,
            unit=f"시간 (p90 {PRIOR_P90_HOURS:g}시간)",
        ),
        Assumption(
            name=f"혼잡 배율 ({congestion})",
            value=CONGESTION_FACTOR[congestion],
            unit="배 (양의 지연에만)",
            source="가정: 선석 혼잡 시 대기가 늘어난다고 봄(elevated 1.25배, high 1.5배)",
        ),
    ]
    if weather != "normal":
        label = {"strong_wind": "풍랑주의보", "typhoon": "태풍"}[weather]
        items.append(
            Assumption(
                name=f"{label} 가산 p50",
                value=add50,
                unit=f"시간 (p90 +{add90:g}시간)",
                source="가정: 태풍 시 부산항 하역 중단 1~2일 + 체선 해소, 풍랑주의보는 반나절"
                if weather == "typhoon"
                else "가정: 풍랑주의보 시 입출항 통제 반나절 안팎",
            )
        )
    return items


class DelayPredictor:
    def __init__(self, records: Iterable[DepartureRecord]) -> None:
        self.records = list(records)
        self._groups: dict[tuple[Level, str], list[float]] = defaultdict(list)
        for r in self.records:
            for key in self._keys(r):
                self._groups[key].append(r.delay_hours)

    @staticmethod
    def _keys(r: DepartureRecord) -> list[tuple[Level, str]]:
        keys: list[tuple[Level, str]] = [("global", "")]
        if r.is_container:
            keys.append(("container", ""))
        if r.operator:
            keys.append(("operator", r.operator))
        if r.terminal:
            keys.append(("terminal", r.terminal))
            keys.append(("terminal_weekday", f"{r.terminal}|{r.weekday}"))
        if vk := vessel_key(r.vessel_name, r.call_sign):
            keys.append(("vessel", vk))
        return keys

    def _chain(self, query: DelayQuery) -> list[tuple[Level, str | None]]:
        terminal = terminal_of(query.terminal)
        chain: list[tuple[Level, str | None]] = [("global", "")]
        if query.container:
            chain.append(("container", ""))
        if op := operator_of(query.vessel_name):
            chain.append(("operator", op))
        if terminal:
            chain.append(("terminal", terminal))
            if query.planned_departure is not None:
                chain.append(
                    ("terminal_weekday", f"{terminal}|{query.planned_departure.weekday()}")
                )
        if vk := vessel_key(query.vessel_name, query.call_sign):
            chain.append(("vessel", vk))
        return chain

    @staticmethod
    def _display(level: Level, key: str | None) -> str | None:
        if level == "terminal_weekday" and key:
            terminal, _, day = key.partition("|")
            return f"{terminal} {WEEKDAYS[int(day)]}요일"
        return key or None

    def base(self, query: DelayQuery) -> tuple[float, float, list[LevelStat]]:
        p50, p90 = PRIOR_P50_HOURS, PRIOR_P90_HOURS
        basis = [
            LevelStat(
                level="prior",
                label=LEVEL_LABELS["prior"],
                key=None,
                sample_size=0,
                p50_hours=p50,
                p90_hours=p90,
                weight=1.0,
            )
        ]
        for level, key in self._chain(query):
            values = self._groups.get((level, key or ""), [])
            if len(values) < MIN_LEVEL_SAMPLES:
                continue
            n = len(values)
            weight = n / (n + SHRINK_K)
            q50, q90 = quantile(values, 0.5), quantile(values, 0.9)
            p50 = weight * q50 + (1 - weight) * p50
            p90 = weight * q90 + (1 - weight) * p90
            basis.append(
                LevelStat(
                    level=level,
                    label=LEVEL_LABELS[level],
                    key=self._display(level, key),
                    sample_size=n,
                    p50_hours=round(q50, 2),
                    p90_hours=round(q90, 2),
                    weight=round(weight, 3),
                )
            )
        return p50, max(p50, p90), basis

    @staticmethod
    def _confidence(basis: list[LevelStat], weather: Weather) -> Confidence:
        if weather == "typhoon":
            return "low"
        top = basis[-1]
        if top.level == "vessel" and top.sample_size >= 5:
            return "high"
        if top.level in ("vessel", "terminal", "terminal_weekday", "operator") and any(
            b.level in ("terminal", "operator", "vessel") and b.sample_size >= 20 for b in basis
        ):
            return "medium"
        return "low"

    def predict(
        self,
        query: DelayQuery,
        weather: Weather = "normal",
        congestion: Congestion = "normal",
    ) -> DelayForecast:
        base50, base90, basis = self.base(query)
        factor = CONGESTION_FACTOR[congestion]
        add50, add90 = WEATHER_HOURS[weather]

        def adjust(value: float, add: float, scale: float = factor) -> float:
            return (value * scale if value > 0 else value) + add

        p50 = adjust(base50, add50)
        p90 = max(p50, adjust(base90, add90))
        plain50 = adjust(base50, add50, 1.0)
        plain90 = max(plain50, adjust(base90, add90, 1.0))
        top = basis[-1]
        where = f"{top.label}" + (f"({top.key})" if top.key else "")
        summary = (
            f"{where} 표본 {top.sample_size}건 기준 지연 p50 {p50:.1f}시간, p90 {p90:.1f}시간"
            if top.sample_size
            else f"표본 없음, 가정 사전값 기준 지연 p50 {p50:.1f}시간, p90 {p90:.1f}시간"
        )
        return DelayForecast(
            p50_hours=round(p50, 1),
            p90_hours=round(p90, 1),
            base_p50_hours=round(base50, 1),
            base_p90_hours=round(base90, 1),
            confidence=self._confidence(basis, weather),
            sample_size=top.sample_size,
            basis=basis,
            assumptions=_assumptions(weather, congestion),
            weather=weather,
            congestion=congestion,
            congestion_factor=factor,
            congestion_added_p50_hours=round(p50 - plain50, 1),
            congestion_added_p90_hours=round(p90 - plain90, 1),
            summary=summary,
        )


@lru_cache
def default_predictor() -> DelayPredictor:
    return DelayPredictor(all_records())
