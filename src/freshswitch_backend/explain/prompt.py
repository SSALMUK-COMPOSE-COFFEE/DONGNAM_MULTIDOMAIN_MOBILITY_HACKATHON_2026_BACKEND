import json

from freshswitch_backend.explain.formatting import (
    change_direction,
    format_amount_krw,
    format_fill,
    format_krw,
)
from freshswitch_backend.explain.models import ExplainContext

SYSTEM_PROMPT = """당신은 부산항 공동 CA 냉장 컨테이너 적재 서비스 '프레시스위치'의 안내 담당자입니다.
화주(소규모 신선 농산물 수출 농가)와 운영자에게 재배치 결과를 설명합니다.

규칙:
1. 숫자는 입력 JSON에 적힌 문자열(예: "12,345원", "2.3만 원", "3→5/20파렛트")을 글자 그대로만 옮겨 씁니다.
2. 변화량(change)은 부호 없는 금액이고 방향은 direction(증가/감소/변동 없음)에 있습니다. 증가면 "2.3만 원 늘어납니다", 감소면 "2.3만 원 줄어듭니다"처럼 쓰고 +, - 부호를 붙이지 않습니다.
3. 입력에 없는 숫자, 합계, 비율, 날짜, 순번은 절대 만들지 않습니다. 계산하지 않습니다. 목록 번호도 쓰지 않습니다.
4. 화주 메시지는 농민이 이해하기 쉬운 존댓말 한국어로 2~3문장, 각 화주의 비용 변화와 화물 이동 여부, pull_forward가 있으면 출하를 그만큼 앞당겨야 한다는 점을 알려줍니다.
5. 운영자 요약은 3문장 이내로 사건, 추천안과 그 이유, 불가능한 대안이 있다면 그 이유를 적습니다.
6. 입력의 모든 화주(shipper_id)에 대해 메시지를 하나씩 만듭니다.
7. 반드시 다음 JSON 형식으로만 답합니다:
{"operator_summary": "...", "shipper_messages": [{"shipper_id": "...", "message": "..."}]}"""

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "operator_summary": {"type": "string"},
        "shipper_messages": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "shipper_id": {"type": "string"},
                    "message": {"type": "string"},
                },
                "required": ["shipper_id", "message"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["operator_summary", "shipper_messages"],
    "additionalProperties": False,
}


def build_payload(ctx: ExplainContext) -> dict:
    return {
        "event_type": ctx.event_type,
        "event_summary": ctx.event_summary,
        "recommended_alternative_id": ctx.recommended_alternative_id,
        "alternatives": [
            {
                "id": alt.id,
                "label": alt.label,
                "feasible": alt.feasible,
                "reason": alt.reason,
                "total_cost_change": format_amount_krw(alt.total_cost_delta_krw),
                "total_cost_direction": change_direction(alt.total_cost_delta_krw),
                "container_fill": [
                    {
                        "container_id": fill.container_id,
                        "fill": format_fill(fill.before, fill.after, fill.capacity),
                    }
                    for fill in alt.container_fill
                ],
            }
            for alt in ctx.alternatives
        ],
        "shippers": [
            {
                "shipper_id": shipper.shipper_id,
                "shipper_name": shipper.shipper_name,
                "cost_before": format_krw(shipper.cost_before_krw),
                "cost_after": format_krw(shipper.cost_after_krw),
                "change": format_amount_krw(shipper.delta_krw),
                "direction": change_direction(shipper.delta_krw),
                "moved": shipper.moved,
                "pull_forward": f"{shipper.pull_forward_days}일"
                if shipper.pull_forward_days
                else None,
            }
            for shipper in ctx.shippers
        ],
    }


def build_user_prompt(ctx: ExplainContext) -> str:
    return json.dumps(build_payload(ctx), ensure_ascii=False, indent=2)
