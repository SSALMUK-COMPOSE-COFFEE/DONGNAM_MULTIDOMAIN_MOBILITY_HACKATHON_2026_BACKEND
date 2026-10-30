from typing import Any, Literal

from fastapi import APIRouter
from pydantic import BaseModel

from freshswitch_backend.config import get_settings
from freshswitch_backend.data.loader import load_scenarios
from freshswitch_backend.integrations import SnapshotDate, SnapshotStore
from freshswitch_backend.integrations.mof_snapshot import load_demo_fixture
from freshswitch_backend.planning.store import store

router = APIRouter(prefix="/demo", tags=["demo"])


class ResetResponse(BaseModel):
    cleared_sessions: int


class DemoStatus(BaseModel):
    offline_mode: bool
    explain_use_llm: bool
    llm_configured: bool
    mof_key_configured: bool
    snapshot_dates: list[SnapshotDate]
    demo_fixture: bool
    scenario_ids: list[str]
    session_count: int


class DemoStep(BaseModel):
    step: int
    id: str
    title: str
    description: str
    method: Literal["GET", "POST"]
    path: str
    body: dict[str, Any] | None = None
    body_from: str | None = None
    expect: str


DELAY_EVENT = {
    "type": "delay",
    "voyage_id": "V-2611A",
    "new_etd": "2026-11-19T06:00:00+09:00",
    "source": "해수부",
}

DEMO_STEPS: list[DemoStep] = [
    DemoStep(
        step=1,
        id="create_plan",
        title="공동적재 계획 생성",
        description="기본 시나리오(딸기 3화주 + 포도 2화주)를 최적화해 컨테이너 배정·집하 경로·정산을 만든다",
        method="POST",
        path="/api/plans",
        body={"scenario_id": "default"},
        expect="201, 응답 plan_id를 이후 단계 {plan_id}에 넣는다",
    ),
    DemoStep(
        step=2,
        id="reduce_sancheong",
        title="산청 딸기 감축 5→3 팔레트",
        description="산청 화주가 출하량을 줄였다. 재최적화 대안 3개(당겨 채우기·다음 항차·그대로 출항)를 받는다",
        method="POST",
        path="/api/plans/{plan_id}/events",
        body={"event": {"type": "reduce", "lot_id": "lot-sancheong-sb-1", "new_pallets": 3}},
        expect="recommended_id = pull_forward",
    ),
    DemoStep(
        step=3,
        id="apply_pull_forward",
        title="당겨 채우기 적용",
        description="빈 자리에 밀양 2차 로트를 하루 당겨 실어 항공 대신 해상으로 보낸다",
        method="POST",
        path="/api/plans/{plan_id}/apply",
        body={"alternative_id": "pull_forward"},
        expect="200, version 2, history 1건",
    ),
    DemoStep(
        step=4,
        id="detect_delay",
        title="해수부 출항 지연 감지",
        description="해수부 입출항 정보(오프라인이면 저장 스냅샷 + 시연 선박)와 예정 ETD를 비교한다",
        method="POST",
        path="/api/plans/{plan_id}/schedule-check",
        expect="changes[0]: V-2611A MV FRESH HARMONY 12시간 지연, events[0]에 delay 이벤트",
    ),
    DemoStep(
        step=5,
        id="replan_delay",
        title="지연 반영 재최적화",
        description="감지된 지연 이벤트를 그대로 보내 대안별 해상 슬롯과 집하 경로를 다시 계산한다",
        method="POST",
        path="/api/plans/{plan_id}/events",
        body={"event": DELAY_EVENT},
        body_from='4단계 응답 events[0]을 {"event": ...}로 감싸 보낸다',
        expect="next_voyage는 판매기한 초과로 실행 불가, recommended_id 확인",
    ),
    DemoStep(
        step=6,
        id="apply_delay_plan",
        title="추천 대안 적용",
        description="재최적화 추천안을 계획에 반영한다",
        method="POST",
        path="/api/plans/{plan_id}/apply",
        body={"alternative_id": "pull_forward"},
        body_from="5단계 응답 recommended_id를 alternative_id로 보낸다",
        expect="200, version 3, history 2건",
    ),
    DemoStep(
        step=7,
        id="compare_air",
        title="항공 전환 비교",
        description="지연 이후에도 밀양 2차 로트를 해상으로 보내는 게 항공보다 싼지 비교한다",
        method="POST",
        path="/api/compare/air-vs-sea",
        body={"scenario_id": "default", "lot_id": "lot-miryang-sb-2", "plan_id": "{plan_id}"},
        expect="cheaper, sea_savings로 절감액 표시",
    ),
]


@router.post("/reset")
def reset() -> ResetResponse:
    return ResetResponse(cleared_sessions=store.clear())


@router.get("/status")
def status() -> DemoStatus:
    settings = get_settings()
    return DemoStatus(
        offline_mode=settings.offline_mode,
        explain_use_llm=settings.explain_use_llm,
        llm_configured=bool(settings.llm_api_key and settings.llm_model_name),
        mof_key_configured=bool(settings.data_go_kr_service_key),
        snapshot_dates=SnapshotStore().dates(),
        demo_fixture=load_demo_fixture() is not None,
        scenario_ids=sorted(load_scenarios()),
        session_count=store.count(),
    )


@router.get("/script")
def script() -> list[DemoStep]:
    return DEMO_STEPS
