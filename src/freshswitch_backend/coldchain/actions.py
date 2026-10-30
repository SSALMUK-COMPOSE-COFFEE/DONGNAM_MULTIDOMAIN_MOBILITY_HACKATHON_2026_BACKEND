from dataclasses import dataclass, field
from datetime import datetime, timedelta

from freshswitch_backend.coldchain.models import (
    ActionKind,
    AlertLevel,
    ColdchainAction,
    ProductBand,
    RequestDraft,
    Stage,
)
from freshswitch_backend.domain.models import Voyage
from freshswitch_backend.execution.orders import TERMINAL_CHANNEL, fmt

TRUCKER = "냉장 운송사(젯셋 보유)"
TRUCKER_CHANNEL = "포워더 → 냉장 운송사"
WAREHOUSE_CHANNEL = "포워더 → 현지 파트너 → 냉장창고"
INSURER = "화주 적하보험사"
INSURER_CHANNEL = "포워더 → 화주 → 적하보험사"
DEFAULT_TERMINAL = "부산신항 터미널"
STAGE_TEXT: dict[Stage, str] = {
    "trucking": "냉장트럭 운송 중",
    "gate": "터미널 게이트 대기",
    "terminal": "터미널 장치장",
    "vessel": "선상",
}
ACTION_TITLE: dict[ActionKind, str] = {
    "terminal_priority_plug": "터미널 우선 플러그 연결 요청",
    "genset_dispatch": "이동형 전원(젯셋) 배정",
    "entry_time_adjust": "반입 시간 조정",
    "cold_storage_booking": "현지 냉장창고 예약 요청",
    "insurance_evidence": "적하보험 청구 입증 자료 준비",
}
ACTION_SEVERITY: dict[ActionKind, AlertLevel] = {
    "terminal_priority_plug": "warning",
    "genset_dispatch": "warning",
    "entry_time_adjust": "warning",
    "cold_storage_booking": "critical",
    "insurance_evidence": "info",
}


@dataclass
class ActionContext:
    container_id: str
    stage: Stage
    voyage: Voyage | None = None
    container_kind: str = "40RH-CA"
    destination: str | None = None
    products: list[ProductBand] = field(default_factory=list)
    band_min_c: float = 0.0
    band_max_c: float = 2.0
    lot_ids: list[str] = field(default_factory=list)
    pallets: int = 0

    @property
    def terminal(self) -> str:
        return self.voyage.terminal if self.voyage else DEFAULT_TERMINAL

    @property
    def vessel(self) -> str:
        if self.voyage is None:
            return ""
        return f" {self.voyage.vessel_name}({self.voyage.id})"

    @property
    def cargo(self) -> str:
        names = "·".join(p.name for p in self.products) or "신선화물"
        pallets = f", {self.pallets}파렛트" if self.pallets else ""
        return f"{self.container_id}({self.container_kind}{pallets}, {names})"

    @property
    def band(self) -> str:
        return f"{self.band_min_c:g}~{self.band_max_c:g}°C"


def _texts(kind: ActionKind, ctx: ActionContext, at: datetime, m: dict) -> tuple[str, RequestDraft]:
    temp = f"{m['temp_c']:.1f}°C"
    continuous = f"{m['continuous']:.0f}분"
    cumulative = f"{m['cumulative']:.0f}분"
    rate = f"{m['rate'] * 100:.1f}%"
    where = STAGE_TEXT[ctx.stage]
    etd = f" ETD {fmt(ctx.voyage.etd)}." if ctx.voyage else ""
    if kind == "terminal_priority_plug":
        return (
            f"{where} 연속 단전 {continuous}, 품온 {temp}. 터미널에 민감 화물 우선 연결을 요청한다",
            RequestDraft(
                counterparty_role="terminal",
                counterparty=ctx.terminal,
                channel=TERMINAL_CHANNEL,
                text=(
                    f"[리퍼 우선 연결 요청] {ctx.terminal}{ctx.vessel} 선적 예정 {ctx.cargo} "
                    f"{fmt(at - timedelta(minutes=m['continuous']))}부터 전원이 끊겨 있습니다"
                    f"(연속 {continuous}, 현재 품온 {temp}, 적정 {ctx.band}). 단전 손실이 큰 "
                    f"민감 신선화물이라 리퍼 플러그 우선 연결과 연결 후 온도 회신 부탁드립니다.{etd}"
                ),
            ),
        )
    if kind == "genset_dispatch":
        return (
            f"연속 단전 {continuous}. 플러그 연결 전까지 이동형 전원으로 냉각을 잇는다",
            RequestDraft(
                counterparty_role="trucker",
                counterparty=TRUCKER,
                channel=TRUCKER_CHANNEL,
                text=(
                    f"[이동형 전원 배정 요청] {ctx.cargo} {where} 중 연속 단전 {continuous}, "
                    f"품온 {temp}(적정 {ctx.band}). 디젤 젯셋(클립온) 1대를 현재 "
                    f"위치({ctx.terminal}, {where})로 즉시 배정해 주십시오. 친환경 대안: 배터리 젯셋(Thermo King "
                    f"E-COOLPAC). 터미널 플러그 연결까지 유지 부탁드립니다."
                ),
            ),
        )
    if kind == "entry_time_adjust":
        cutoff = f" 반입 마감 {fmt(ctx.voyage.cy_cutoff)}." if ctx.voyage else ""
        return (
            f"누적 단전 {cumulative}. 반입·선적 시각을 플러그 확보 시각에 맞춰 다시 잡는다",
            RequestDraft(
                counterparty_role="terminal",
                counterparty=ctx.terminal,
                channel=TERMINAL_CHANNEL,
                text=(
                    f"[반입 시간 조정 요청] {ctx.terminal}{ctx.vessel} {ctx.cargo} 누적 단전 "
                    f"{cumulative}. 리퍼 플러그가 확보되는 시각에 맞춰 반입(장치) 시각을 다시 "
                    f"잡아 주십시오. 그 전까지 젯셋을 연결한 채 대기합니다.{cutoff}{etd}"
                ),
            ),
        )
    if kind == "cold_storage_booking":
        destination = ctx.destination or "도착지"
        arrival = ""
        if ctx.voyage:
            eta = ctx.voyage.etd + timedelta(days=ctx.voyage.transit_days)
            arrival = f" 도착 예정 {fmt(eta)}."
        return (
            f"누적 단전 {cumulative}, 예상 손실률 {rate}. 도착 즉시 입고할 현지 냉장창고를 잡는다",
            RequestDraft(
                counterparty_role="destination_warehouse",
                counterparty=f"{destination} 현지 냉장창고",
                channel=WAREHOUSE_CHANNEL,
                text=(
                    f"[현지 냉장창고 예약 요청] {destination}행 {ctx.cargo}.{arrival} 운송 중 "
                    f"누적 단전 {cumulative}으로 예상 손실률이 {rate}입니다. 도착 즉시 입고·재예냉"
                    f"({ctx.band}) 가능한 냉장창고 예약과 입고 검수(손실률 측정) 부탁드립니다."
                ),
            ),
        )
    return (
        f"연속 단전 {continuous}. 냉동기 고장 특약(연속 8시간) 청구 요건에 든다",
        RequestDraft(
            counterparty_role="insurer",
            counterparty=INSURER,
            channel=INSURER_CHANNEL,
            text=(
                f"[적하보험 냉동기 고장 특약 청구 자료] {ctx.cargo} 연속 단전 {continuous}"
                f"(누적 {cumulative}), 최고 품온 기록 포함 센서 기록 {m['readings']}건을 "
                f"청구 입증 자료로 첨부합니다."
            ),
        ),
    )


def build_action(
    kind: ActionKind, key: str, ctx: ActionContext, at: datetime, metrics: dict
) -> ColdchainAction:
    description, request = _texts(kind, ctx, at, metrics)
    return ColdchainAction(
        key=key,
        kind=kind,
        severity=ACTION_SEVERITY[kind],
        title=f"{ctx.container_id} {ACTION_TITLE[kind]}",
        description=description,
        container_id=ctx.container_id,
        triggered_at=at,
        lot_ids=ctx.lot_ids,
        request=request,
    )
