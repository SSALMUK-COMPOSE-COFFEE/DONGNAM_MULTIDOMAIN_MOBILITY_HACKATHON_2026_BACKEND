import asyncio
import json
import time
from typing import Any

from httpx import HTTPError
from openai import AsyncOpenAI, BadRequestError, OpenAIError

from freshswitch_backend.config import Settings, get_settings
from freshswitch_backend.explain.formatting import extract_numbers
from freshswitch_backend.explain.models import ExplainContext, Explanation, ShipperMessage
from freshswitch_backend.explain.prompt import OUTPUT_SCHEMA, SYSTEM_PROMPT, build_user_prompt
from freshswitch_backend.explain.template import render_template
from freshswitch_backend.explain.validation import shipper_error, summary_error
from freshswitch_backend.observability import record_event

LLM_TIMEOUT_SECONDS = 8.0


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
        if shipper_id in messages:
            raise ExplanationRejected("화주 메시지 중복")
        messages[shipper_id] = message.strip()
    expected = [s.shipper_id for s in ctx.shippers]
    if set(messages) != set(expected):
        raise ExplanationRejected("화주 목록 불일치")
    if reason := summary_error(ctx, summary):
        raise ExplanationRejected(reason)
    for shipper in ctx.shippers:
        if reason := shipper_error(ctx, shipper, messages[shipper.shipper_id]):
            raise ExplanationRejected(f"{shipper.shipper_id}: {reason}")
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
    usage = getattr(response, "usage", None)
    if usage is not None:
        record_event(
            "ai.usage",
            model=model,
            **{
                name: value
                for name in ("prompt_tokens", "completion_tokens", "total_tokens")
                if isinstance(value := getattr(usage, name, None), int)
            },
        )
    return response.choices[0].message.content or ""


async def explain(
    ctx: ExplainContext,
    *,
    settings: Settings | None = None,
    client: Any | None = None,
    timeout_s: float = LLM_TIMEOUT_SECONDS,
) -> Explanation:
    settings = settings or get_settings()
    model = settings.llm_model_name
    started = time.perf_counter()

    def finish(result: Explanation, error_type: str | None = None) -> Explanation:
        record_event(
            "ai.explanation",
            model=model,
            event_type=ctx.event_type,
            source=result.generated_by,
            fallback_reason=result.fallback_reason,
            elapsed_ms=round((time.perf_counter() - started) * 1000, 2),
            budget_seconds=timeout_s,
            shipper_count=len(ctx.shippers),
            error_type=error_type,
        )
        return result

    def fallback(reason: str) -> Explanation:
        return finish(render_template(ctx).model_copy(update={"fallback_reason": reason}))

    timeout_s = min(timeout_s, settings.explain_timeout_seconds)
    if timeout_s <= 0:
        return fallback("timeout")
    owned_client = False
    if client is None:
        if not settings.llm_api_key or not model:
            return fallback("unavailable")
        client = AsyncOpenAI(
            api_key=settings.llm_api_key,
            base_url=settings.llm_base_url,
            timeout=timeout_s,
            max_retries=0,
        )
        owned_client = True
    try:
        content = await asyncio.wait_for(_complete(client, model, ctx), timeout=timeout_s)
        return finish(validate_output(ctx, json.loads(content), model))
    except TimeoutError:
        return fallback("timeout")
    except (OpenAIError, ValueError, TypeError, AttributeError, IndexError) as exc:
        # Provider exception messages may echo API keys or authorization headers.
        record_event("ai.explanation_error", error_type=type(exc).__name__)
        return fallback("provider_error" if isinstance(exc, OpenAIError) else "validation")
    finally:
        if owned_client:
            try:
                await client.close()
            except (OpenAIError, HTTPError, RuntimeError, OSError) as exc:
                record_event("ai.client_close_error", error_type=type(exc).__name__)
