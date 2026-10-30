import json
import re
from typing import Any, Literal

from fastapi import APIRouter, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from freshswitch_backend.config import get_settings
from freshswitch_backend.data.loader import load_scenarios
from freshswitch_backend.integrations import SnapshotDate, SnapshotStore
from freshswitch_backend.integrations.mof_snapshot import load_demo_fixture
from freshswitch_backend.planning.store import store

router = APIRouter(prefix="/demo", tags=["demo"])

DEFAULT_CURL_BASE = "https://freshswitch.ssalhub.xyz/api"


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


class DemoCapture(BaseModel):
    name: str
    path: str
    kind: Literal["string", "json"] = "string"


class DemoCheck(BaseModel):
    path: str
    op: Literal["eq", "contains", "len", "gt"] = "eq"
    value: Any


class DemoStep(BaseModel):
    step: int
    id: str
    title: str
    description: str
    method: Literal["GET", "POST"]
    path: str
    body: dict[str, Any] | None = None
    body_from: str | None = None
    capture: list[DemoCapture] = Field(default_factory=list)
    checks: list[DemoCheck] = Field(default_factory=list)
    expect: str


def _confirm_shipper(
    step_id: str, shipper_id: str, label: str, order_var: str, status: str, stage: str
) -> dict[str, Any]:
    return {
        "id": step_id,
        "title": f"{label} 화주 수락 ({stage})",
        "description": f"{label} 화주가 실행 지시의 비용·일정 변경을 수락한다",
        "method": "POST",
        "path": "/api/plans/{plan_id}/orders/{" + order_var + "}/confirm",
        "body": {"actor": "shipper", "shipper_id": shipper_id, "accept": True},
        "body_from": f"경로의 {{{order_var}}}는 앞 실행 지시 조회 응답 마지막 항목의 order_id",
        "checks": [{"path": ".status", "value": status}],
        "expect": f"200, status {status}",
    }


def _confirm_forwarder(step_id: str, order_var: str, stage: str) -> dict[str, Any]:
    return {
        "id": step_id,
        "title": f"포워더 확정 ({stage})",
        "description": "화주 확인이 끝난 지시를 포워더가 확정한다. 계획이 committed가 되고 자동 조치가 실행된다",
        "method": "POST",
        "path": "/api/plans/{plan_id}/orders/{" + order_var + "}/confirm",
        "body": {"actor": "forwarder", "accept": True},
        "body_from": f"경로의 {{{order_var}}}는 앞 실행 지시 조회 응답 마지막 항목의 order_id",
        "checks": [
            {"path": ".status", "value": "forwarder_confirmed"},
            {"path": ".automatic[0].done", "value": True},
        ],
        "expect": "200, status forwarder_confirmed, automatic[].done = true",
    }


DELAY_SHIPPERS = [
    ("sh-jinju", "진주"),
    ("sh-miryang", "밀양"),
    ("sh-pohang", "포항"),
    ("sh-sancheong", "산청"),
    ("sh-sangju", "상주"),
]

_STEPS: list[dict[str, Any]] = [
    {
        "id": "reset",
        "title": "시연 초기화",
        "description": "메모리의 계획 세션을 모두 지우고 처음부터 시작한다",
        "method": "POST",
        "path": "/api/demo/reset",
        "expect": "200, cleared_sessions",
    },
    {
        "id": "intake_strawberry",
        "title": "간편 출하 등록: 산청 딸기 2파렛트",
        "description": "품목·수량·출하일·선과장·목적지 5개만 넣으면 판매기한·로트 ID를 채우고 "
        "해상 컨테이너별·항공 가능 여부를 바로 알려준다(미리보기, 세션은 안 바뀜)",
        "method": "POST",
        "path": "/api/intake/lots",
        "body": {
            "product": "딸기",
            "pallets": 2,
            "ready_date": "2026-11-17",
            "shipper": "산청",
            "destination": "홍콩",
        },
        "checks": [
            {"path": ".resolved.shipper_id", "value": "sh-sancheong"},
            {"path": ".defaults_applied", "op": "len", "value": 2},
            {"path": ".hints.earliest_feasible.container_id", "value": "CA-01"},
            {"path": ".hints.earliest_feasible.arrival_date", "value": "2026-11-21"},
            {"path": ".hints.air.feasible", "value": True},
            {"path": ".hints.warnings", "op": "len", "value": 0},
            {"path": ".event.type", "value": "add_lot"},
        ],
        "expect": "sell_by_date·lot_id 자동 채움, 가장 빠른 해상 CA-01(V-2611A, 11/21 도착), "
        "항공도 가능, 경고 없음",
    },
    {
        "id": "intake_grape_so2",
        "title": "간편 출하 등록: 포항 샤인머스캣(SO2) 3파렛트",
        "description": "SO2 처리 포도는 딸기와 같은 컨테이너에 못 싣는다는 경고를 미리보기에서 보여준다",
        "method": "POST",
        "path": "/api/intake/lots",
        "body": {
            "product": "샤인머스캣",
            "pallets": 3,
            "ready_date": "2026-11-17",
            "shipper": "포항",
            "destination": "홍콩",
        },
        "checks": [
            {"path": ".resolved.product_id", "value": "grape_so2"},
            {"path": ".hints.warnings[0]", "op": "contains", "value": "별도 CA 컨테이너"},
        ],
        "expect": "warnings[0]: SO2 처리 품목은 ... 별도 CA 컨테이너가 필요합니다",
    },
    {
        "id": "create_plan",
        "title": "공동적재 계획 생성",
        "description": "기본 시나리오(딸기 3화주 + 포도 2화주)를 최적화해 컨테이너 배정·집하 경로·정산을 만든다",
        "method": "POST",
        "path": "/api/plans",
        "body": {"scenario_id": "default"},
        "capture": [{"name": "plan_id", "path": ".plan_id"}],
        "checks": [
            {"path": ".version", "value": 1},
            {"path": ".status", "value": "committed"},
            {"path": ".plan.total_savings", "value": 9987059},
        ],
        "expect": "201, 절감 9,987,059원. plan_id를 이후 단계 {plan_id}에 넣는다",
    },
    {
        "id": "reduce_sancheong",
        "title": "산청 딸기 감축 5→3 팔레트",
        "description": "산청 화주가 출하량을 줄였다. 재최적화 대안 3개(당겨 채우기·다음 항차·그대로 출항)를 "
        "받는다. idempotency_key로 같은 요청을 다시 보내도 결과가 같다",
        "method": "POST",
        "path": "/api/plans/{plan_id}/events?llm=false",
        "body": {
            "event": {"type": "reduce", "lot_id": "lot-sancheong-sb-1", "new_pallets": 3},
            "idempotency_key": "demo-reduce-sancheong",
        },
        "checks": [
            {"path": ".alternatives", "op": "len", "value": 3},
            {"path": ".recommended_id", "value": "pull_forward"},
            {"path": ".alternatives[1].id", "value": "next_voyage"},
            {"path": ".alternatives[1].feasible", "value": False},
            {"path": ".alternatives[2].id", "value": "sail_as_is"},
            {
                "path": ".alternatives[2].diff.cancellation.payer_shipper_id",
                "value": "sh-sancheong",
            },
            {"path": ".alternatives[2].diff.cancellation.fee", "value": 852706},
            {"path": ".alternatives[0].diff.cancellation.fee", "value": 0},
            {"path": ".explanation.generated_by", "value": "template"},
        ],
        "expect": "대안 3개, 추천 pull_forward, next_voyage는 판매기한 초과로 불가, "
        "그대로 출항이면 산청 취소 부담금 852,706원(당겨 채우면 0원), 템플릿 설명문",
    },
    {
        "id": "apply_pull_forward",
        "title": "당겨 채우기 적용",
        "description": "빈 자리에 밀양 2차 로트를 하루 당겨 실어 항공 대신 해상으로 보낸다. 실행 지시가 생긴다",
        "method": "POST",
        "path": "/api/plans/{plan_id}/apply",
        "body": {"alternative_id": "pull_forward"},
        "checks": [
            {"path": ".version", "value": 2},
            {"path": ".status", "value": "pending_confirmation"},
        ],
        "expect": "200, version 2, status pending_confirmation",
    },
    {
        "id": "orders_pull_forward",
        "title": "실행 지시 확인",
        "description": "확인할 화주(진주·밀양), 자동 조치, 포워더가 선사·터미널·항공사에 보낼 요청 문장",
        "method": "GET",
        "path": "/api/plans/{plan_id}/orders",
        "capture": [{"name": "order_id", "path": ".[-1].order_id"}],
        "checks": [
            {"path": ".[-1].status", "value": "proposed"},
            {"path": ".[-1].shipper_confirmations", "op": "len", "value": 2},
            {"path": ".[-1].requests[0].kind", "value": "booking_amendment"},
            {"path": ".[-1].requests[0].text", "op": "contains", "value": "[부킹 변경 요청]"},
            {"path": ".[-1].requests[1].kind", "value": "cy_entry"},
            {"path": ".[-1].requests[2].kind", "value": "air_cancel"},
            {"path": ".[-1].requests[2].text", "op": "contains", "value": "[항공 예약 취소 요청]"},
        ],
        "expect": "requests: 부킹 변경(선사)·CY 반입(터미널)·항공 취소(항공사). order_id를 이후 {order_id}에",
    },
    _confirm_shipper("confirm_jinju", "sh-jinju", "진주", "order_id", "proposed", "감축"),
    _confirm_shipper(
        "confirm_miryang", "sh-miryang", "밀양", "order_id", "shipper_confirmed", "감축"
    ),
    _confirm_forwarder("confirm_forwarder", "order_id", "감축"),
    {
        "id": "plan_committed",
        "title": "계획 확정 확인",
        "description": "포워더 확정 뒤 세션이 committed, 확정 버전 2가 된다",
        "method": "GET",
        "path": "/api/plans/{plan_id}",
        "checks": [
            {"path": ".status", "value": "committed"},
            {"path": ".committed_version", "value": 2},
        ],
        "expect": "status committed, committed_version 2",
    },
    {
        "id": "detect_delay",
        "title": "해수부 출항 지연 감지",
        "description": "해수부 입출항 정보(오프라인이면 저장 스냅샷)와 시연 선박 실적을 예정 ETD와 비교한다",
        "method": "POST",
        "path": "/api/plans/{plan_id}/schedule-check?demo=true",
        "capture": [{"name": "delay_event", "path": ".events[0]", "kind": "json"}],
        "checks": [
            {"path": ".changes[0].voyage_id", "value": "V-2611A"},
            {"path": ".changes[0].vessel_name", "value": "MV FRESH HARMONY"},
            {"path": ".changes[0].delta_hours", "value": 12},
            {"path": ".events[0].type", "value": "delay"},
        ],
        "expect": "changes[0]: V-2611A MV FRESH HARMONY 12시간 지연, events[0]에 delay 이벤트",
    },
    {
        "id": "replan_delay",
        "title": "지연 반영 재최적화",
        "description": "감지된 지연 이벤트를 그대로 보내 대안별 해상 슬롯과 집하 경로를 다시 계산한다",
        "method": "POST",
        "path": "/api/plans/{plan_id}/events?llm=false",
        "body": {"event": "{delay_event}"},
        "body_from": "{delay_event}는 앞 단계 응답 events[0] 객체 그대로",
        "capture": [{"name": "delay_alternative", "path": ".recommended_id"}],
        "checks": [
            {"path": ".alternatives", "op": "len", "value": 3},
            {"path": ".recommended_id", "value": "pull_forward"},
            {"path": ".alternatives[1].feasible", "value": False},
        ],
        "expect": "next_voyage는 판매기한 초과로 실행 불가, 추천 pull_forward",
    },
    {
        "id": "apply_delay_plan",
        "title": "지연 추천안 적용",
        "description": "재최적화 추천안을 계획에 반영한다. 새 실행 지시가 생긴다",
        "method": "POST",
        "path": "/api/plans/{plan_id}/apply",
        "body": {"alternative_id": "{delay_alternative}"},
        "body_from": "{delay_alternative}는 앞 단계 응답 recommended_id",
        "checks": [
            {"path": ".version", "value": 3},
            {"path": ".status", "value": "pending_confirmation"},
        ],
        "expect": "200, version 3, history 2건",
    },
    {
        "id": "orders_delay",
        "title": "지연 실행 지시 확인",
        "description": "지연으로 보관비가 늘어 5개 화주 모두 확인 대상, 터미널에 리퍼 전원 연장 요청",
        "method": "GET",
        "path": "/api/plans/{plan_id}/orders",
        "capture": [{"name": "delay_order_id", "path": ".[-1].order_id"}],
        "checks": [
            {"path": ".[-1].status", "value": "proposed"},
            {"path": ".[-1].shipper_confirmations", "op": "len", "value": len(DELAY_SHIPPERS)},
            {"path": ".[-1].requests[0].kind", "value": "reefer_plug"},
            {
                "path": ".[-1].requests[0].text",
                "op": "contains",
                "value": "[리퍼 전원 연결 연장 요청]",
            },
        ],
        "expect": "화주 확인 5건(cost_increase), 요청 reefer_plug 2건(CA-01·CA-02). "
        "order_id를 이후 {delay_order_id}에",
    },
    *[
        _confirm_shipper(
            f"confirm_delay_{shipper_id.removeprefix('sh-')}",
            shipper_id,
            label,
            "delay_order_id",
            "shipper_confirmed" if i == len(DELAY_SHIPPERS) - 1 else "proposed",
            "지연",
        )
        for i, (shipper_id, label) in enumerate(DELAY_SHIPPERS)
    ],
    _confirm_forwarder("confirm_delay_forwarder", "delay_order_id", "지연"),
    {
        "id": "compare_air",
        "title": "항공 전환 비교",
        "description": "지연 이후에도 밀양 2차 로트를 해상으로 보내는 게 항공보다 싼지 비교한다",
        "method": "POST",
        "path": "/api/compare/air-vs-sea",
        "body": {"scenario_id": "default", "lot_id": "lot-miryang-sb-2", "plan_id": "{plan_id}"},
        "checks": [
            {"path": ".sea.in_plan", "value": True},
            {"path": ".cheaper", "value": "sea"},
            {"path": ".sea_savings", "value": 164164},
        ],
        "expect": "cheaper sea, 해상이 164,164원 싸다",
    },
    {
        "id": "benchmark",
        "title": "수작업 규칙 대비 최적화",
        "description": "선착순 채우기 등 수작업 규칙과 최적화의 컨테이너 수·절감액을 같은 비용 기준으로 비교한다",
        "method": "POST",
        "path": "/api/analysis/benchmark",
        "body": {"scenario_id": "default"},
        "checks": [
            {"path": ".methods[0].method", "value": "optimizer"},
            {"path": ".methods[0].containers_used", "value": 2},
            {"path": ".methods[0].total_savings", "value": 9987059},
            {"path": ".methods[1].method", "value": "fcfs"},
            {"path": ".methods[1].containers_used", "value": 3},
            {"path": ".methods[1].total_savings", "value": 2154542},
        ],
        "expect": "최적화 컨테이너 2대·절감 9,987,059원 vs 선착순 3대·2,154,542원",
    },
    {
        "id": "business_matrix",
        "title": "BM 시나리오 매트릭스",
        "description": "성과보수율·기본 이용료·포워더 배분·고정비·항공 운임 조합 96개의 1년 차 손익",
        "method": "GET",
        "path": "/api/analysis/business/matrix?scenario_id=default",
        "checks": [
            {"path": ".rows", "op": "len", "value": 96},
            {"path": ".break_even_rows", "value": 48},
            {"path": ".highlights[0].summary", "op": "contains", "value": "흑자"},
        ],
        "expect": "96행 중 흑자 48행, highlights[0]: MVP 현금 고정비면 흑자",
    },
    {
        "id": "arrival_settlement",
        "title": "도착 검수 품질 사후정산",
        "description": "산청 로트 홍콩 도착 검수 손실률이 계획 가정(지연 반영 4.2%)을 넘으면 "
        "넘은 손실액만큼 성과보수를 환급한다",
        "method": "POST",
        "path": "/api/plans/{plan_id}/settlement/arrival",
        "body": {
            "lot_id": "lot-sancheong-sb-1",
            "inspected_loss_pct": 4.5,
            "note": "홍콩 도착 검수",
        },
        "checks": [
            {"path": ".planned_loss_rate", "value": 0.042},
            {"path": ".refund_after", "op": "gt", "value": 0},
            {"path": ".capped", "value": False},
            {"path": ".platform_net_change", "op": "gt", "value": -200000},
        ],
        "expect": "계획 4.2% 대비 0.3%p 초과 손실만큼 산청 성과보수 환급(한도 안)",
    },
]

DEMO_STEPS: list[DemoStep] = [DemoStep(step=i, **s) for i, s in enumerate(_STEPS)]


def _sq(text: str) -> str:
    return text.replace("'", "'\\''")


def _shell_body(body: dict[str, Any], kinds: dict[str, str]) -> str:
    text = _sq(json.dumps(body, ensure_ascii=False, separators=(",", ":")))
    for name, kind in kinds.items():
        if kind == "json":
            text = text.replace(f'"{{{name}}}"', f"'\"${{{name}}}\"'")
        else:
            text = text.replace(f"{{{name}}}", f"'\"${{{name}}}\"'")
    return f"'{text}'"


def _shell_path(path: str) -> str:
    path = path.removeprefix("/api")
    return '"' + re.sub(r"\{(\w+)\}", r"${\1}", path) + '"'


CURL_HEADER = """#!/usr/bin/env bash
set -euo pipefail
BASE=${BASE:-%s}
command -v jq >/dev/null || { echo "jq가 필요합니다" >&2; exit 1; }
command -v curl >/dev/null || { echo "curl이 필요합니다" >&2; exit 1; }
RESP=""
MISMATCH=0

call() {
  local method=$1 path=$2 body=${3-} out code
  if [ -n "$body" ]; then
    out=$(curl -sS -X "$method" "$BASE$path" -H 'content-type: application/json' -d "$body" -w '\\n%%{http_code}')
  else
    out=$(curl -sS -X "$method" "$BASE$path" -w '\\n%%{http_code}')
  fi
  code=${out##*$'\\n'}
  RESP=${out%%$'\\n'*}
  if [ "${code:0:1}" != "2" ]; then
    echo "  실패 HTTP $code: $RESP" >&2
    exit 1
  fi
  echo "  HTTP $code"
}

capture() {
  local value
  value=$(jq "$2" "$3" <<<"$RESP")
  if [ -z "$value" ] || [ "$value" = "null" ]; then
    echo "  값을 못 찾음: $3" >&2
    exit 1
  fi
  printf -v "$1" '%%s' "$value"
  echo "  $1 = $value"
}

check() {
  local path=$1 op=$2 want=$3 expr
  case $op in
    eq) expr="$path == \\$v" ;;
    contains) expr="($path) as \\$x | if (\\$x|type) == \\"array\\" then (\\$x|index(\\$v)) != null else (\\$x|tostring|contains(\\$v)) end" ;;
    len) expr="($path|length) == \\$v" ;;
    gt) expr="$path > \\$v" ;;
  esac
  if jq -e --argjson v "$want" "$expr" <<<"$RESP" >/dev/null; then
    echo "  ok $path $op $want"
  else
    echo "  불일치 $path $op $want (실제 $(jq -c "$path" <<<"$RESP"))"
    MISMATCH=$((MISMATCH + 1))
  fi
}
"""


def curl_script(base: str = DEFAULT_CURL_BASE) -> str:
    kinds: dict[str, str] = {}
    lines = [CURL_HEADER % base]
    for step in DEMO_STEPS:
        lines.append(f"echo '== [{step.step}] {_sq(step.title)}'")
        call = f"call {step.method} {_shell_path(step.path)}"
        if step.body is not None:
            call += " " + _shell_body(step.body, kinds)
        lines.append(call)
        for cap in step.capture:
            flag = "-r" if cap.kind == "string" else "-c"
            lines.append(f"capture {cap.name} {flag} '{cap.path}'")
            kinds[cap.name] = cap.kind
        for chk in step.checks:
            want = _sq(json.dumps(chk.value, ensure_ascii=False))
            lines.append(f"check '{chk.path}' {chk.op} '{want}'")
        lines.append("")
    lines.append('echo "== 끝: 불일치 $MISMATCH건"')
    lines.append('[ "$MISMATCH" -eq 0 ]')
    return "\n".join(lines) + "\n"


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


@router.get("/script", response_model=list[DemoStep])
def script(format: Literal["json", "curl"] = Query("json")) -> Any:
    if format == "curl":
        return PlainTextResponse(curl_script(), media_type="text/x-shellscript; charset=utf-8")
    return DEMO_STEPS
