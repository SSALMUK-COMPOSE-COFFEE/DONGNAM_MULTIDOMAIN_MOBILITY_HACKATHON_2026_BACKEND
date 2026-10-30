from datetime import date
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from freshswitch_backend.execution.cancellation import annotate_cancellations
from freshswitch_backend.forecast.predictor import default_predictor
from freshswitch_backend.forecast.service import (
    AirSwitchProposal,
    ForecastOptions,
    VoyageForecast,
    air_switch_proposals,
    delay_event,
    forecast_voyages,
)
from freshswitch_backend.planning.store import store
from freshswitch_backend.replan import DelayEvent, ReplanResult, locations, replan
from freshswitch_backend.replan.explanation import explain_replan, llm_enabled

router = APIRouter(prefix="/plans", tags=["forecast"])


class HistoryInfo(BaseModel):
    record_count: int
    start: date | None
    end: date | None
    source: str = "해수부 선박입출항 출항 신고(부산항): 출항예정일시 대비 출항일시"


class DelayForecastReplan(ReplanResult):
    plan_id: str
    replan_id: str
    base_version: int


class DelayForecastResponse(BaseModel):
    plan_id: str
    options: ForecastOptions
    history: HistoryInfo
    voyages: list[VoyageForecast]
    at_risk_lot_ids: list[str]
    triggered: bool
    event: DelayEvent | None
    air_switch: list[AirSwitchProposal]
    air_lot_ids: list[str]
    replan: DelayForecastReplan | None
    summary: str


def _history() -> HistoryInfo:
    records = default_predictor().records
    days = [r.departure_at.date() for r in records]
    return HistoryInfo(
        record_count=len(records), start=min(days, default=None), end=max(days, default=None)
    )


def _summary(
    voyages: list[VoyageForecast], proposals: list[AirSwitchProposal], air: list[str]
) -> str:
    parts = [
        f"{v.vessel_name}({v.voyage_id}) {v.quantile} 지연 {v.delay_hours:g}시간" for v in voyages
    ]
    head = ", ".join(parts)
    if not proposals:
        return f"{head}. 판매기한·신선 유지 기간을 넘는 로트 없음"
    lots = ", ".join(p.lot_id for p in proposals)
    tail = f" → 재계획 추천안에서 항공 전환: {', '.join(air)}" if air else ""
    return f"{head}. 위험 로트 {lots}{tail}"


@router.post("/{plan_id}/delay-forecast")
async def delay_forecast(
    plan_id: str,
    body: ForecastOptions | None = None,
    llm: Annotated[bool | None, Query()] = None,
) -> DelayForecastResponse:
    session = store.get(plan_id)
    if session is None:
        raise HTTPException(status_code=404, detail="계획을 찾을 수 없습니다")
    options = (body or ForecastOptions()).resolved()
    voyages = forecast_voyages(default_predictor(), session.scenario, session.plan, options)
    event = delay_event(voyages)
    replan_response = None
    where: dict[str, str] = {}
    air: list[str] = []
    if event is not None:
        result = await run_in_threadpool(
            replan,
            session.scenario,
            session.plan,
            event,
            air_locked=session.air_locked_lot_ids,
        )
        annotate_cancellations(result, session.scenario, session.plan)
        result.explanation = await explain_replan(
            session.scenario, session.plan, result, use_llm=llm_enabled(llm)
        )
        pending = store.add_pending(session, result)
        replan_response = DelayForecastReplan(
            **{name: getattr(result, name) for name in ReplanResult.model_fields},
            plan_id=plan_id,
            replan_id=pending.replan_id,
            base_version=pending.base_version,
        )
        chosen = next((a for a in result.alternatives if a.id == result.recommended_id), None)
        if chosen is not None:
            before = locations(session.plan)
            where = locations(chosen.plan)
            air = sorted(
                lid for lid, loc in where.items() if loc == "AIR" and before.get(lid) != "AIR"
            )
    proposals = air_switch_proposals(session.scenario, voyages, where)
    return DelayForecastResponse(
        plan_id=plan_id,
        options=options,
        history=_history(),
        voyages=voyages,
        at_risk_lot_ids=[p.lot_id for p in proposals],
        triggered=event is not None,
        event=event,
        air_switch=proposals,
        air_lot_ids=air,
        replan=replan_response,
        summary=_summary(voyages, proposals, air),
    )
