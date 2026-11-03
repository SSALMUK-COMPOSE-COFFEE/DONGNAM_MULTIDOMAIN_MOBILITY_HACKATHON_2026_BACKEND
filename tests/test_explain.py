import asyncio
import json
import os
from types import SimpleNamespace

import httpx
import pytest
from openai import APIConnectionError

from freshswitch_backend.config import Settings
from freshswitch_backend.explain import (
    ExplainContext,
    explain,
    format_delta_krw,
    format_krw,
    render_template,
)
from freshswitch_backend.explain.formatting import format_amount_krw
from freshswitch_backend.explain.prompt import build_payload


def make_context() -> ExplainContext:
    return ExplainContext.model_validate(
        {
            "event_type": "reduce",
            "event_summary": "산청 화주 5→3파렛트 감축",
            "recommended_alternative_id": "pull_forward",
            "alternatives": [
                {
                    "id": "pull_forward",
                    "label": "대기 화물 당겨 싣기",
                    "feasible": True,
                    "reason": None,
                    "total_cost_delta_krw": -23_000,
                    "container_fill": [
                        {"container_id": "CA-01", "before": 18, "after": 20, "capacity": 20}
                    ],
                },
                {
                    "id": "next_voyage",
                    "label": "다음 항차로 연기",
                    "feasible": False,
                    "reason": "신선도 한계 초과",
                    "total_cost_delta_krw": 0,
                    "container_fill": [],
                },
            ],
            "shippers": [
                {
                    "shipper_id": "s-sancheong",
                    "shipper_name": "산청 딸기농가",
                    "cost_before_krw": 412_000,
                    "cost_after_krw": 389_000,
                    "delta_krw": -23_000,
                    "moved": None,
                },
                {
                    "shipper_id": "s-jinju",
                    "shipper_name": "진주 파프리카",
                    "cost_before_krw": 250_000,
                    "cost_after_krw": 253_500,
                    "delta_krw": 3_500,
                    "moved": "CA-01",
                },
            ],
        }
    )


SETTINGS = Settings(llm_api_key="test-key", llm_model_name="fake-model", _env_file=None)


class FakeCompletions:
    def __init__(self, content: str | None = None, error: Exception | None = None) -> None:
        self.content = content
        self.error = error
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        message = SimpleNamespace(content=self.content)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def fake_client(completions: FakeCompletions) -> SimpleNamespace:
    return SimpleNamespace(chat=SimpleNamespace(completions=completions))


def llm_payload(summary: str, jinju_message: str) -> str:
    return json.dumps(
        {
            "operator_summary": summary,
            "shipper_messages": [
                {
                    "shipper_id": "s-sancheong",
                    "message": render_template(make_context()).shipper_messages[0].message,
                },
                {"shipper_id": "s-jinju", "message": jinju_message},
            ],
        },
        ensure_ascii=False,
    )


def test_formatting() -> None:
    assert format_krw(12_345) == "12,345원"
    assert format_delta_krw(23_000) == "+2.3만 원"
    assert format_delta_krw(-20_000) == "-2만 원"
    assert format_delta_krw(3_500) == "+3,500원"
    assert format_delta_krw(0) == "변동 없음"
    assert format_amount_krw(-23_000) == "2.3만 원"
    assert format_amount_krw(3_500) == "3,500원"
    assert format_amount_krw(-1_991_430) == "199.1만 원"


def test_payload_uses_unsigned_change_and_direction() -> None:
    payload = build_payload(make_context())
    sancheong, jinju = payload["shippers"]
    assert (sancheong["change"], sancheong["direction"]) == ("2.3만 원", "감소")
    assert (jinju["change"], jinju["direction"]) == ("3,500원", "증가")
    alt = payload["alternatives"][0]
    assert (alt["total_cost_change"], alt["total_cost_direction"]) == ("2.3만 원", "감소")
    assert "+" not in json.dumps(payload, ensure_ascii=False)


def test_llm_success() -> None:
    completions = FakeCompletions(
        llm_payload(
            render_template(make_context()).operator_summary,
            render_template(make_context()).shipper_messages[1].message,
        )
    )
    result = asyncio.run(
        explain(make_context(), settings=SETTINGS, client=fake_client(completions))
    )
    assert result.generated_by == "llm"
    assert result.model == "fake-model"
    assert [m.shipper_id for m in result.shipper_messages] == ["s-sancheong", "s-jinju"]
    request = completions.calls[0]
    assert request["model"] == "fake-model"
    assert "253,500원" in request["messages"][1]["content"]


@pytest.mark.parametrize(
    "old,new",
    [
        ("3,500원", "2.3만 원"),
        ("늘어납니다", "줄어듭니다"),
        ("250,000원에서 253,500원", "253,500원에서 250,000원"),
        ("CA-01", "CA-02"),
        ("화물은 CA-01(으)로 옮겨집니다.", "화물은 CA-01로 이동하지 않습니다."),
        ("진주 파프리카", "산청 딸기농가"),
        ("화물은 CA-01(으)로 옮겨집니다.", "화물은 CA-01(으)로 옮겨집니다. 출하를 1일 앞당깁니다."),
    ],
)
def test_shipper_facts_are_scoped_and_grounded(old, new):
    message = render_template(make_context()).shipper_messages[1].message.replace(old, new)
    completions = FakeCompletions(
        llm_payload(render_template(make_context()).operator_summary, message)
    )
    result = asyncio.run(
        explain(make_context(), settings=SETTINGS, client=fake_client(completions))
    )
    assert result.generated_by == "template"
    assert result.fallback_reason == "validation"


@pytest.mark.parametrize(
    "delta,moved,pull",
    [
        (0, None, 0),
        (-3500, "CA-01", 2),
        (3500, "항공", 0),
    ],
)
def test_canonical_facts_pass_for_zero_decrease_air_and_pull(delta, moved, pull):
    ctx = make_context()
    shipper = ctx.shippers[1]
    shipper.delta_krw = delta
    shipper.cost_after_krw = shipper.cost_before_krw + delta
    shipper.moved = moved
    shipper.pull_forward_days = pull
    payload = render_template(ctx).model_dump(include={"operator_summary", "shipper_messages"})
    result = asyncio.run(
        explain(ctx, settings=SETTINGS, client=fake_client(FakeCompletions(json.dumps(payload))))
    )
    assert result.generated_by == "llm"


def test_duplicate_shipper_is_rejected():
    ctx = make_context()
    payload = json.loads(llm_payload(render_template(ctx).operator_summary, "message"))
    payload["shipper_messages"].append(payload["shipper_messages"][0])
    result = asyncio.run(
        explain(ctx, settings=SETTINGS, client=fake_client(FakeCompletions(json.dumps(payload))))
    )
    assert result.fallback_reason == "validation"


def test_changed_recommendation_is_rejected():
    ctx = make_context()
    payload = render_template(ctx).model_dump(include={"operator_summary", "shipper_messages"})
    payload["operator_summary"] = payload["operator_summary"].replace(
        "대기 화물 당겨 싣기", "다음 항차로 연기"
    )
    result = asyncio.run(
        explain(ctx, settings=SETTINGS, client=fake_client(FakeCompletions(json.dumps(payload))))
    )
    assert result.fallback_reason == "validation"


def test_missing_pull_forward_is_rejected():
    ctx = make_context()
    ctx.shippers[1].pull_forward_days = 2
    payload = render_template(ctx).model_dump(include={"operator_summary", "shipper_messages"})
    payload["shipper_messages"][1]["message"] = payload["shipper_messages"][1]["message"].replace(
        "출하를 2일 앞당겨 주셔야 합니다.", ""
    )
    result = asyncio.run(
        explain(ctx, settings=SETTINGS, client=fake_client(FakeCompletions(json.dumps(payload))))
    )
    assert result.fallback_reason == "validation"


def test_slow_llm_is_cancelled_and_returns_template():
    cancelled = []

    class SlowCompletions:
        async def create(self, **kwargs):
            try:
                await asyncio.sleep(60)
            finally:
                cancelled.append(True)

    result = asyncio.run(
        explain(
            make_context(), settings=SETTINGS, client=fake_client(SlowCompletions()), timeout_s=0.01
        )
    )
    assert result.generated_by == "template"
    assert result.fallback_reason == "timeout"
    assert cancelled == [True]


def test_owned_llm_client_is_closed_after_validation_failure(monkeypatch):
    from freshswitch_backend.explain import service

    closed = []
    client = fake_client(FakeCompletions("invalid-json"))

    async def close():
        closed.append(True)

    client.close = close
    monkeypatch.setattr(service, "AsyncOpenAI", lambda **kwargs: client)
    result = asyncio.run(explain(make_context(), settings=SETTINGS))
    assert result.fallback_reason == "validation"
    assert closed == [True]


def test_invented_number_falls_back_to_template() -> None:
    completions = FakeCompletions(
        llm_payload("총 7개 컨테이너를 재배치합니다.", "비용이 4,000원 늘어납니다.")
    )
    result = asyncio.run(
        explain(make_context(), settings=SETTINGS, client=fake_client(completions))
    )
    assert result.generated_by == "template"


def test_missing_shipper_falls_back_to_template() -> None:
    content = json.dumps({"operator_summary": "요약", "shipper_messages": []})
    completions = FakeCompletions(content)
    result = asyncio.run(
        explain(make_context(), settings=SETTINGS, client=fake_client(completions))
    )
    assert result.generated_by == "template"


def test_client_error_falls_back_to_template() -> None:
    completions = FakeCompletions(
        error=APIConnectionError(request=httpx.Request("POST", "http://llm.test"))
    )
    result = asyncio.run(
        explain(make_context(), settings=SETTINGS, client=fake_client(completions))
    )
    assert result.generated_by == "template"


def test_invalid_json_falls_back_to_template() -> None:
    completions = FakeCompletions("not json")
    result = asyncio.run(
        explain(make_context(), settings=SETTINGS, client=fake_client(completions))
    )
    assert result.generated_by == "template"


def test_no_key_uses_template() -> None:
    settings = Settings(llm_api_key="", llm_model_name="fake-model", _env_file=None)
    result = asyncio.run(explain(make_context(), settings=settings))
    assert result.generated_by == "template"
    assert result.model is None


def test_template_mentions_each_shipper_and_delta() -> None:
    result = render_template(make_context())
    by_id = {m.shipper_id: m.message for m in result.shipper_messages}
    assert "산청 딸기농가" in by_id["s-sancheong"]
    assert "389,000원으로 2.3만 원 줄어듭니다." in by_id["s-sancheong"]
    assert "-2.3" not in by_id["s-sancheong"]
    assert "412,000원" in by_id["s-sancheong"]
    assert "진주 파프리카" in by_id["s-jinju"]
    assert "253,500원으로 3,500원 늘어납니다." in by_id["s-jinju"]
    assert "+3,500" not in by_id["s-jinju"]
    assert "총 비용은 2.3만 원 줄어듭니다." in result.operator_summary
    assert "CA-01" in by_id["s-jinju"]
    assert "대기 화물 당겨 싣기" in result.operator_summary
    assert "신선도 한계 초과" in result.operator_summary
    assert "산청 딸기농가" in result.operator_summary
    assert "진주 파프리카" in result.operator_summary


@pytest.mark.skipif(not os.environ.get("LLM_API_KEY"), reason="LLM_API_KEY 미설정")
def test_live_llm() -> None:
    result = asyncio.run(explain(make_context(), settings=Settings(_env_file=None)))
    print(result.model_dump_json(indent=2))
    assert len(result.shipper_messages) == 2
    assert result.generated_by == "llm"
