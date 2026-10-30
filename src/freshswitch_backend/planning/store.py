import threading
import uuid
from collections import OrderedDict
from datetime import UTC, datetime

from pydantic import BaseModel

from freshswitch_backend.domain.models import Plan, Scenario
from freshswitch_backend.explain.models import Explanation
from freshswitch_backend.planning.service import VoyagePickup, build_plan
from freshswitch_backend.replan import AirSwitchEvent, CancelEvent, Event, ReplanResult
from freshswitch_backend.replan.models import AlternativeId

MAX_SESSIONS = 200
MAX_PENDING = 20


class SessionConflict(Exception):
    pass


class HistoryEntry(BaseModel):
    seq: int
    replan_id: str
    event: Event
    alternative_id: AlternativeId
    applied_at: datetime
    cost_before: int
    cost_after: int
    cost_delta: int
    solve_ms: int
    explanation: Explanation | None = None


class PlanSession(BaseModel):
    plan_id: str
    scenario_id: str
    version: int
    created_at: datetime
    updated_at: datetime
    scenario: Scenario
    plan: Plan
    routes: list[VoyagePickup]
    air_locked_lot_ids: list[str]
    history: list[HistoryEntry]


class PendingReplan(BaseModel):
    replan_id: str
    base_version: int
    result: ReplanResult


def _now() -> datetime:
    return datetime.now(UTC)


class SessionStore:
    def __init__(self, max_sessions: int = MAX_SESSIONS) -> None:
        self._sessions: OrderedDict[str, PlanSession] = OrderedDict()
        self._pending: dict[str, OrderedDict[str, PendingReplan]] = {}
        self._lock = threading.RLock()
        self._max = max_sessions

    def create(self, scenario: Scenario) -> PlanSession:
        working = scenario.model_copy(deep=True)
        built = build_plan(working)
        now = _now()
        session = PlanSession(
            plan_id=str(uuid.uuid4()),
            scenario_id=scenario.id,
            version=1,
            created_at=now,
            updated_at=now,
            scenario=working,
            plan=built.plan,
            routes=built.routes,
            air_locked_lot_ids=[],
            history=[],
        )
        with self._lock:
            self._sessions[session.plan_id] = session
            self._pending[session.plan_id] = OrderedDict()
            while len(self._sessions) > self._max:
                old, _ = self._sessions.popitem(last=False)
                self._pending.pop(old, None)
        return session

    def get(self, plan_id: str) -> PlanSession | None:
        with self._lock:
            return self._sessions.get(plan_id)

    def count(self) -> int:
        with self._lock:
            return len(self._sessions)

    def clear(self) -> int:
        with self._lock:
            cleared = len(self._sessions)
            self._sessions.clear()
            self._pending.clear()
            return cleared

    def add_pending(self, session: PlanSession, result: ReplanResult) -> PendingReplan:
        pending = PendingReplan(
            replan_id=str(uuid.uuid4()), base_version=session.version, result=result
        )
        with self._lock:
            queue = self._pending.setdefault(session.plan_id, OrderedDict())
            queue[pending.replan_id] = pending
            while len(queue) > MAX_PENDING:
                queue.popitem(last=False)
        return pending

    def apply(self, plan_id: str, alternative_id: str, replan_id: str | None = None) -> PlanSession:
        with self._lock:
            session = self._sessions[plan_id]
            queue = self._pending.get(plan_id) or OrderedDict()
            if replan_id is None:
                if not queue:
                    raise SessionConflict("적용할 재최적화 결과가 없습니다. 먼저 이벤트를 보내세요")
                pending = next(reversed(queue.values()))
            elif replan_id in queue:
                pending = queue[replan_id]
            else:
                raise KeyError(replan_id)
            if pending.base_version != session.version:
                raise SessionConflict(
                    "이미 다른 대안이 적용된 계획입니다. 이벤트를 다시 보내 재최적화하세요"
                )
            alternative = next(
                (a for a in pending.result.alternatives if a.id == alternative_id), None
            )
            if alternative is None:
                raise KeyError(alternative_id)
            if not alternative.feasible:
                raise SessionConflict(
                    f"실행 불가한 대안입니다: {'; '.join(alternative.infeasible_reasons)}"
                )
            event = pending.result.event
            scenario = pending.result.scenario_after
            locked = set(session.air_locked_lot_ids)
            if isinstance(event, AirSwitchEvent):
                locked.add(event.lot_id)
            if isinstance(event, CancelEvent):
                locked.discard(event.lot_id)
            entry = HistoryEntry(
                seq=len(session.history) + 1,
                replan_id=pending.replan_id,
                event=event,
                alternative_id=alternative.id,
                applied_at=_now(),
                cost_before=session.plan.total_shared_cost,
                cost_after=alternative.total_cost,
                cost_delta=alternative.cost_delta,
                solve_ms=pending.result.solve_ms,
                explanation=pending.result.explanation,
            )
            updated = session.model_copy(
                update={
                    "version": session.version + 1,
                    "updated_at": entry.applied_at,
                    "scenario": scenario,
                    "plan": alternative.plan,
                    "routes": alternative.routes,
                    "air_locked_lot_ids": sorted(locked),
                    "history": [*session.history, entry],
                }
            )
            self._sessions[plan_id] = updated
            return updated


store = SessionStore()
