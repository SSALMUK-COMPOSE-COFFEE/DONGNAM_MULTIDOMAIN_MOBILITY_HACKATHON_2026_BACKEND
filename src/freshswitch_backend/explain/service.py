import asyncio
import json
import logging
from typing import Any

from openai import AsyncOpenAI, BadRequestError, OpenAIError

from freshswitch_backend.config import Settings, get_settings
from freshswitch_backend.explain.formatting import extract_numbers
from freshswitch_backend.explain.models import ExplainContext, Explanation, ShipperMessage
from freshswitch_backend.explain.prompt import OUTPUT_SCHEMA, SYSTEM_PROMPT, build_user_prompt
from freshswitch_backend.explain.template import render_template

logger = logging.getLogger(__name__)

LLM_TIMEOUT_SECONDS = 12.0


class ExplanationRejected(ValueError):
    pass


def allowed_numbers(ctx: ExplainContext) -> set[str]:
    return extract_numbers(build_user_prompt(ctx))


def validate_output(ctx: ExplainContext, data: Any, model: str) -> Explanation:
    if not isinstance(data, dict):
        raise ExplanationRejected("응답이 JSON 객체가 아님")
    summary = data.get("operator_summary")
    raw_messages = data.get("shipper_messages")
    if not isinstance(summary, str) or not summary.strip() or not isinstance(raw_messages, list):
        raise ExplanationRejected("필수 필드 누락")
    messages: dict[str, str] = {}
    for item in raw_messages:
        if not isinstance(item, dict):
            raise ExplanationRejected("메시지 형식 오류")
        shipper_id, message = item.get("shipper_id"), item.get("message")
        if not isinstance(shipper_id, str) or not isinstance(message, str) or not message.strip():
            raise ExplanationRejected("메시지 형식 오류")
        messages[shipper_id] = message.strip()
    expected = [s.shipper_id for s in ctx.shippers]
    if set(messages) != set(expected):
        raise ExplanationRejected("화주 목록 불일치")
    allowed = allowed_numbers(ctx)
    for text in [summary, *messages.values()]:
        invented = extract_numbers(text) - allowed
        if invented:
            raise ExplanationRejected(f"입력에 없는 숫자: {sorted(invented)}")
    return Explanation(
        operator_summary=summary.strip(),
        shipper_messages=[
            ShipperMessage(shipper_id=sid, message=messages[sid]) for sid in expected
        ],
        generated_by="llm",
        model=model,
    )


async def _complete(client: Any, model: str, ctx: ExplainContext) -> str:
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": build_user_prompt(ctx)},
    ]
    schema_format = {
        "type": "json_schema",
        "json_schema": {"name": "explanation", "strict": True, "schema": OUTPUT_SCHEMA},
    }
    try:
        response = await client.chat.completions.create(
            model=model, messages=messages, response_format=schema_format
        )
    except BadRequestError:
        response = await client.chat.completions.create(
            model=model, messages=messages, response_format={"type": "json_object"}
        )
    return response.choices[0].message.content or ""


async def explain(
    ctx: ExplainContext,
    *,
    settings: Settings | None = None,
    client: Any | None = None,
) -> Explanation:
    settings = settings or get_settings()
    model = settings.llm_model_name
    if client is None:
        if not settings.llm_api_key or not model:
            return render_template(ctx)
        client = AsyncOpenAI(
            api_key=settings.llm_api_key,
            base_url=settings.llm_base_url,
            timeout=LLM_TIMEOUT_SECONDS,
            max_retries=0,
        )
    try:
        content = await asyncio.wait_for(_complete(client, model, ctx), timeout=LLM_TIMEOUT_SECONDS)
        return validate_output(ctx, json.loads(content), model)
    except (OpenAIError, TimeoutError, ValueError, TypeError, AttributeError, IndexError) as exc:
        logger.warning("LLM 설명 실패, 템플릿으로 대체: %s", exc)
        return render_template(ctx)
