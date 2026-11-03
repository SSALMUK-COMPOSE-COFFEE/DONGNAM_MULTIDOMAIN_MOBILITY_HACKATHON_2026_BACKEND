import threading
import uuid
from collections import OrderedDict
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from freshswitch_backend.domain.models import Plan, Scenario
from freshswitch_backend.execution.models import (
    TERMINAL_STATUSES,
    ConfirmRequest,
    ExecutionOrder,
    OrderLogEntry,
    OrderStatus,
    RequestResultBody,
)
from freshswitch_backend.execution.orders import build_order
from freshswitch_backend.execution.quality import (
    ArrivalInspection,
    ArrivalRequest,
    ArrivalSettlementResult,
    arrival_settlement,
    current_inspections,
    planned_loss_rate,
)
from freshswitch_backend.explain.models import Explanation
from freshswitch_backend.planning.service import VoyagePickup, build_plan
from freshswitch_backend.replan import (
    Alternative,
    CancelEvent,
    Event,
    ReplanResult,
    diff_plans,
    locks_air,
    plan_score,
)
from freshswitch_backend.replan.models import AlternativeId, CancellationCharge

MAX_SESSIONS = 200
MAX_PENDING = 20
MAX_ORDERS = 50
MAX_IDEMPOTENCY_KEYS = 100

SessionStatus = Literal["committed", "pending_confirmation"]


class SessionConflict(Exception):
    pass


class OrderInputError(ValueError):
    pass


class IdempotencyMismatch(ValueError):
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
    kind: Literal["apply", "rollback"] = "apply"
    order_id: str | None = None


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
    status: SessionStatus = "committed"
    committed_version: int = 1
    active_order_id: str | None = None
    arrival_inspections: list[ArrivalInspection] = Field(default_factory=list)
    inspection_revision: int = 0


class PendingReplan(BaseModel):
    replan_id: str
    base_version: int
    result: ReplanResult


class _Snapshot(BaseModel):
    version: int
    scenario: Scenario
    plan: Plan
    routes: list[VoyagePickup]
    air_locked_lot_ids: list[str]
    arrival_inspections: list[ArrivalInspection] = Field(default_factory=list)


def _now() -> datetime:
    return datetime.now(UTC)


def _snapshot(session: PlanSession) -> _Snapshot:
    return _Snapshot(
        version=session.version,
        scenario=session.scenario,
        plan=session.plan,
        routes=session.routes,
        air_locked_lot_ids=session.air_locked_lot_ids,
        arrival_inspections=session.arrival_inspections,
    )


def _cumulative(
    committed: _Snapshot,
    scenario_after: Scenario,
    alternative: Alternative,
    superseded: list[ExecutionOrder],
) -> Alternative:
    diff = diff_plans(scenario_after, committed.plan, alternative.plan)
    charges: list[CancellationCharge] = [
        o.cancellation for o in superseded if o.cancellation and o.cancellation.fee
    ]
    if alternative.diff.cancellation and alternative.diff.cancellation.fee:
        charges.append(alternative.diff.cancellation)
    adjustments: dict[str, int] = {}
    for charge in charges:
        payer = charge.payer_shipper_id
        adjustments[payer] = adjustments.get(payer, 0) + charge.fee
        for transfer in charge.transfers:
            sid = transfer.shipper_id
            adjustments[sid] = adjustments.get(sid, 0) - transfer.amount
    for delta in diff.shippers:
        delta.cancellation_adjustment = adjustments.get(delta.shipper_id, 0)
    diff.cancellation = charges[-1] if charges else alternative.diff.cancellation
    return alternative.model_copy(update={"diff": diff})


class SessionStore:
    def __init__(self, max_sessions: int = MAX_SESSIONS) -> None:
        self._sessions: OrderedDict[str, PlanSession] = OrderedDict()
        self._pending: dict[str, OrderedDict[str, PendingReplan]] = {}
        self._orders: dict[str, OrderedDict[str, ExecutionOrder]] = {}
        self._committed: dict[str, _Snapshot] = {}
        self._idempotency: dict[str, OrderedDict[str, tuple[str, str, Any]]] = {}
        self._lock = threading.RLock()
        self._max = max_sessions
        self.clock: Callable[[], datetime] = _now

    def create(self, scenario: Scenario) -> PlanSession:
        working = scenario.model_copy(deep=True)
        built = build_plan(working)
        now = self.clock()
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
            self._orders[session.plan_id] = OrderedDict()
            self._committed[session.plan_id] = _snapshot(session)
            self._idempotency[session.plan_id] = OrderedDict()
            while len(self._sessions) > self._max:
                old, _ = self._sessions.popitem(last=False)
                self._drop(old)
        return session

    def _drop(self, plan_id: str) -> None:
        self._pending.pop(plan_id, None)
        self._orders.pop(plan_id, None)
        self._committed.pop(plan_id, None)
        self._idempotency.pop(plan_id, None)

    def get(self, plan_id: str) -> PlanSession | None:
        with self._lock:
            if plan_id in self._sessions:
                self._expire_due(plan_id)
            return self._sessions.get(plan_id)

    def count(self) -> int:
        with self._lock:
            return len(self._sessions)

    def clear(self) -> int:
        with self._lock:
            cleared = len(self._sessions)
            self._sessions.clear()
            self._pending.clear()
            self._orders.clear()
            self._committed.clear()
            self._idempotency.clear()
            return cleared

    def idempotent(self, plan_id: str, key: str, kind: str, fingerprint: str) -> Any | None:
        with self._lock:
            stored = (self._idempotency.get(plan_id) or {}).get(key)
            if stored is None:
                return None
            stored_kind, stored_fingerprint, value = stored
            if stored_kind != kind or stored_fingerprint != fingerprint:
                raise IdempotencyMismatch(
                    "같은 idempotency_key로 다른 요청이 이미 처리됐습니다. 새 키를 쓰세요"
                )
            return value

    def remember(self, plan_id: str, key: str, kind: str, fingerprint: str, value: Any) -> Any:
        with self._lock:
            keys = self._idempotency.setdefault(plan_id, OrderedDict())
            if key in keys:
                return self.idempotent(plan_id, key, kind, fingerprint)
            keys[key] = (kind, fingerprint, value)
            while len(keys) > MAX_IDEMPOTENCY_KEYS:
                keys.popitem(last=False)
            return value

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

    def apply(
        self,
        plan_id: str,
        alternative_id: str,
        replan_id: str | None = None,
        idempotency: tuple[str, str] | None = None,
    ) -> PlanSession:
        with self._lock:
            session = self._sessions[plan_id]
            if idempotency is not None:
                replayed = self.idempotent(plan_id, idempotency[0], "apply", idempotency[1])
                if replayed is not None:
                    return replayed
            self._expire_due(plan_id)
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
            if locks_air(scenario, event):
                locked.add(event.lot_id)
            if isinstance(event, CancelEvent):
                locked.discard(event.lot_id)
            now = self.clock()
            orders = self._orders.setdefault(plan_id, OrderedDict())
            superseded = [
                o for o in orders.values() if o.status in ("proposed", "shipper_confirmed")
            ]
            for previous in superseded:
                self._transition(previous, "expired", now, "system", "새 대안 적용으로 대체됨")
                previous.expired_reason = "superseded"
            committed = self._committed[plan_id]
            before = session
            ordered = alternative
            if committed.version != session.version:
                before = committed
                ordered = _cumulative(committed, scenario, alternative, superseded)
            order = build_order(
                plan_id=plan_id,
                replan_id=pending.replan_id,
                base_version=session.version,
                target_version=session.version + 1,
                rollback_version=committed.version,
                scenario_before=before.scenario,
                plan_before=before.plan,
                routes_before=before.routes,
                scenario_after=scenario,
                alternative=ordered,
                event=event,
                now=now,
            )
            entry = HistoryEntry(
                seq=len(session.history) + 1,
                replan_id=pending.replan_id,
                event=event,
                alternative_id=alternative.id,
                applied_at=now,
                cost_before=plan_score(session.plan),
                cost_after=alternative.total_cost,
                cost_delta=alternative.cost_delta,
                solve_ms=pending.result.solve_ms,
                explanation=pending.result.explanation,
                order_id=order.order_id,
            )
            updated = session.model_copy(
                update={
                    "version": session.version + 1,
                    "updated_at": now,
                    "scenario": scenario,
                    "plan": alternative.plan,
                    "routes": alternative.routes,
                    "air_locked_lot_ids": sorted(locked),
                    "history": [*session.history, entry],
                    "status": "pending_confirmation",
                    "active_order_id": order.order_id,
                    "arrival_inspections": self._carry_inspections(
                        session, scenario, alternative.plan
                    ),
                }
            )
            self._sessions[plan_id] = updated
            orders[order.order_id] = order
            while len(orders) > MAX_ORDERS:
                orders.popitem(last=False)
            if idempotency is not None:
                self.remember(plan_id, idempotency[0], "apply", idempotency[1], updated)
            return updated

    def orders(self, plan_id: str) -> list[ExecutionOrder]:
        with self._lock:
            self._expire_due(plan_id)
            return list((self._orders.get(plan_id) or {}).values())

    def order(self, plan_id: str, order_id: str) -> ExecutionOrder:
        with self._lock:
            self._expire_due(plan_id)
            return self._orders[plan_id][order_id]

    def _transition(
        self,
        order: ExecutionOrder,
        status: OrderStatus,
        now: datetime,
        actor: str,
        action: str,
        shipper_id: str | None = None,
        note: str | None = None,
    ) -> None:
        order.status = status
        order.updated_at = now
        order.log.append(
            OrderLogEntry(
                at=now,
                actor=actor,
                action=action,
                status_after=status,
                shipper_id=shipper_id,
                note=note,
            )
        )

    def _expire_due(self, plan_id: str) -> None:
        now = self.clock()
        for order in (self._orders.get(plan_id) or {}).values():
            if order.status == "proposed" and now > order.deadlines.shipper:
                deadline = order.deadlines.shipper
            elif order.status == "shipper_confirmed" and now > order.deadlines.forwarder:
                deadline = order.deadlines.forwarder
            else:
                continue
            self._transition(order, "expired", now, "system", "확인 기한 초과로 만료")
            order.expired_reason = f"deadline {deadline.isoformat()}"
            self._rollback(plan_id, order, now)

    def _rollback(self, plan_id: str, order: ExecutionOrder, now: datetime) -> None:
        session = self._sessions.get(plan_id)
        if session is None or session.version != order.target_version:
            return
        snapshot = self._committed[plan_id]
        version = session.version + 1
        entry = HistoryEntry(
            seq=len(session.history) + 1,
            replan_id=order.replan_id,
            event=order.event,
            alternative_id=order.alternative_id,
            applied_at=now,
            cost_before=plan_score(session.plan),
            cost_after=plan_score(snapshot.plan),
            cost_delta=plan_score(snapshot.plan) - plan_score(session.plan),
            solve_ms=0,
            kind="rollback",
            order_id=order.order_id,
        )
        self._sessions[plan_id] = session.model_copy(
            update={
                "version": version,
                "updated_at": now,
                "scenario": snapshot.scenario,
                "plan": snapshot.plan,
                "routes": snapshot.routes,
                "air_locked_lot_ids": snapshot.air_locked_lot_ids,
                "history": [*session.history, entry],
                "status": "committed",
                "committed_version": version,
                "active_order_id": None,
                "arrival_inspections": snapshot.arrival_inspections,
            }
        )
        self._committed[plan_id] = snapshot.model_copy(update={"version": version})
        order.rolled_back_to_version = snapshot.version

    def _commit(self, plan_id: str, order: ExecutionOrder, now: datetime) -> None:
        session = self._sessions.get(plan_id)
        if session is None or session.version != order.target_version:
            return
        updated = session.model_copy(
            update={
                "updated_at": now,
                "status": "committed",
                "committed_version": session.version,
            }
        )
        self._sessions[plan_id] = updated
        self._committed[plan_id] = _snapshot(updated)

    def confirm(self, plan_id: str, order_id: str, body: ConfirmRequest) -> ExecutionOrder:
        with self._lock:
            self._expire_due(plan_id)
            order = self._orders[plan_id][order_id]
            now = self.clock()
            if body.actor == "shipper":
                return self._confirm_shipper(plan_id, order, body, now)
            return self._confirm_forwarder(plan_id, order, body, now)

    def _confirm_shipper(
        self, plan_id: str, order: ExecutionOrder, body: ConfirmRequest, now: datetime
    ) -> ExecutionOrder:
        if not body.shipper_id:
            raise OrderInputError("화주 확인에는 shipper_id가 필요합니다")
        item = next(
            (c for c in order.shipper_confirmations if c.shipper_id == body.shipper_id), None
        )
        if item is None:
            raise OrderInputError(f"이 실행 지시에서 확인할 화주가 아닙니다: {body.shipper_id}")
        decision = "accepted" if body.accept else "rejected"
        if item.status != "pending":
            if item.status == decision:
                return order
            raise SessionConflict(f"이미 {item.status} 처리된 화주 확인입니다")
        if order.status != "proposed":
            raise SessionConflict(f"화주 확인을 받을 수 없는 상태입니다: {order.status}")
        item.status = decision
        item.note = body.note
        item.decided_at = now
        if not body.accept:
            self._transition(
                order,
                "rejected",
                now,
                "shipper",
                "화주 거절 → 이전 확정 계획으로 복귀",
                body.shipper_id,
                body.note,
            )
            self._rollback(plan_id, order, now)
            return order
        if all(c.status == "accepted" for c in order.shipper_confirmations):
            self._transition(
                order,
                "shipper_confirmed",
                now,
                "shipper",
                "모든 화주 확인 완료",
                body.shipper_id,
                body.note,
            )
        else:
            order.updated_at = now
            order.log.append(
                OrderLogEntry(
                    at=now,
                    actor="shipper",
                    action="화주 수락",
                    status_after=order.status,
                    shipper_id=body.shipper_id,
                    note=body.note,
                )
            )
        return order

    def _confirm_forwarder(
        self, plan_id: str, order: ExecutionOrder, body: ConfirmRequest, now: datetime
    ) -> ExecutionOrder:
        if order.status in ("forwarder_confirmed", "executed") and body.accept:
            return order
        if order.status in TERMINAL_STATUSES:
            raise SessionConflict(f"이미 종료된 실행 지시입니다: {order.status}")
        if order.status != "shipper_confirmed":
            raise SessionConflict("화주 확인이 끝나지 않아 포워더가 확정할 수 없습니다")
        if not body.accept:
            self._transition(
                order,
                "rejected",
                now,
                "forwarder",
                "포워더 거절 → 이전 확정 계획으로 복귀",
                note=body.note,
            )
            self._rollback(plan_id, order, now)
            return order
        self._transition(
            order,
            "forwarder_confirmed",
            now,
            "forwarder",
            "포워더 확정 → 계획 확정, 자동 조치 실행, 제3자 요청 발송",
            note=body.note,
        )
        for action in order.automatic:
            action.done = True
        self._commit(plan_id, order, now)
        self._finish_if_done(plan_id, order, now)
        return order

    def _finish_if_done(self, plan_id: str, order: ExecutionOrder, now: datetime) -> None:
        if order.status != "forwarder_confirmed":
            return
        if all(r.status == "accepted" for r in order.requests):
            self._transition(order, "executed", now, "system", "모든 요청 회신 완료 → 실행 완료")
            session = self._sessions.get(plan_id)
            if session and session.active_order_id == order.order_id:
                self._sessions[plan_id] = session.model_copy(update={"active_order_id": None})

    def answer_request(
        self, plan_id: str, order_id: str, request_id: str, body: RequestResultBody
    ) -> ExecutionOrder:
        with self._lock:
            self._expire_due(plan_id)
            order = self._orders[plan_id][order_id]
            request = next((r for r in order.requests if r.request_id == request_id), None)
            if request is None:
                raise KeyError(request_id)
            decision = "accepted" if body.accepted else "declined"
            if request.status != "pending":
                if request.status == decision:
                    return order
                raise SessionConflict(f"이미 {request.status} 처리된 요청입니다")
            if order.status != "forwarder_confirmed":
                raise SessionConflict("포워더 확정 뒤에만 요청 회신을 기록할 수 있습니다")
            now = self.clock()
            request.status = decision
            request.note = body.note
            request.answered_at = now
            order.updated_at = now
            order.log.append(
                OrderLogEntry(
                    at=now,
                    actor="forwarder",
                    action=(
                        f"{request.request_id} {request.counterparty} 수락"
                        if body.accepted
                        else f"{request.request_id} {request.counterparty} 거절 → 재계획 필요"
                    ),
                    status_after=order.status,
                    note=body.note,
                )
            )
            self._finish_if_done(plan_id, order, now)
            return order

    def record_arrival(self, plan_id: str, body: ArrivalRequest) -> ArrivalSettlementResult:
        return self.record_arrival_snapshot(plan_id, body)[0]

    def record_arrival_snapshot(
        self, plan_id: str, body: ArrivalRequest
    ) -> tuple[ArrivalSettlementResult, PlanSession]:
        with self._lock:
            self._expire_due(plan_id)
            session = self._sessions[plan_id]
            now = self.clock()
            result, inspections = arrival_settlement(
                plan_id, session.scenario, session.plan, session.arrival_inspections, body, now
            )
            updated = session.model_copy(
                update={
                    "arrival_inspections": inspections,
                    "updated_at": now,
                    "inspection_revision": session.inspection_revision + 1,
                }
            )
            self._sessions[plan_id] = updated
            if updated.status == "committed":
                self._committed[plan_id] = _snapshot(updated)
            return result, updated.model_copy(deep=True)

    @staticmethod
    def _carry_inspections(session: PlanSession, scenario: Scenario, plan: Plan):
        before = {a.lot_id: a for a in session.plan.assignments}
        after = {a.lot_id: a for a in plan.assignments}
        lots_before = {lot.id: lot for lot in session.scenario.lots}
        lots_after = {lot.id: lot for lot in scenario.lots}
        kept = []
        for inspection in current_inspections(plan, session.arrival_inspections):
            lid = inspection.lot_id
            if before.get(lid) != after.get(lid) or lots_before.get(lid) != lots_after.get(lid):
                continue
            if planned_loss_rate(session.scenario, session.plan, lid) != planned_loss_rate(
                scenario, plan, lid
            ):
                continue
            kept.append(inspection)
        return kept


store = SessionStore()
