import threading
import uuid
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from freshswitch_backend.coldchain.models import (
    Alert,
    AlertFeed,
    Binding,
    BindingRequest,
    IngestRejected,
    IngestResult,
    Reading,
    ReadingIn,
    SettlementPreview,
    SettlementPreviewLot,
    SimulationRequest,
    SimulationRun,
    Stage,
    TimelinePoint,
    TwinState,
    TwinSummary,
)
from freshswitch_backend.coldchain.params import ColdchainParams
from freshswitch_backend.coldchain.simulator import PRESETS, preset_readings
from freshswitch_backend.coldchain.twin import (
    Carry,
    Evaluation,
    TwinContext,
    build_context,
    downsample,
    evaluate,
    loss_summary,
)
from freshswitch_backend.execution.quality import (
    ArrivalRequest,
    QualityError,
    arrival_settlement,
)
from freshswitch_backend.planning.store import PlanSession
from freshswitch_backend.planning.store import store as plan_store

MAX_READINGS = 5000
MAX_HISTORY = 1000
MAX_ALERTS = 2000
MAX_CONTAINERS = 200
DEFAULT_STAGE: Stage = "terminal"
PREVIEW_RULE = (
    "트윈 예상 손실률을 도착 검수 손실률 대신 넣어 품질 사후정산을 미리 계산한다. "
    "저장하지 않는다. 실제 환급은 도착 검수(POST /settlement/arrival)로 확정한다"
)


class ColdchainError(ValueError):
    pass


class NotFound(LookupError):
    pass


@dataclass
class ContainerRecord:
    container_id: str
    plan_id: str | None = None
    stage: Stage = DEFAULT_STAGE
    readings: dict[tuple[str, datetime], Reading] = field(default_factory=dict)
    alerts: OrderedDict[str, Alert] = field(default_factory=OrderedDict)
    carry: Carry | None = None
    history: list[TimelinePoint] = field(default_factory=list)
    anchors: set[datetime] = field(default_factory=set)

    def sorted_readings(self) -> list[Reading]:
        return sorted(self.readings.values(), key=lambda r: (r.ts, r.device_id))


@dataclass
class _Run:
    info: SimulationRun
    readings: list[ReadingIn]
    stop: threading.Event
    thread: threading.Thread | None = None


def _now() -> datetime:
    return datetime.now(UTC)


class ColdchainStore:
    def __init__(self, params: ColdchainParams | None = None) -> None:
        self.params = params or ColdchainParams()
        self._bindings: dict[str, Binding] = {}
        self._records: OrderedDict[tuple[str | None, str], ContainerRecord] = OrderedDict()
        self._feed: list[Alert] = []
        self._seq = 0
        self._runs: dict[str, _Run] = {}
        self._lock = threading.RLock()
        self.clock: Callable[[], datetime] = _now

    def clear(self) -> int:
        with self._lock:
            for run in self._runs.values():
                run.stop.set()
            count = len(self._records)
            self._bindings.clear()
            self._records.clear()
            self._feed.clear()
            self._runs.clear()
            return count

    def _session(self, plan_id: str | None) -> PlanSession | None:
        return plan_store.get(plan_id) if plan_id else None

    def require_plan(self, plan_id: str | None, container_id: str | None = None) -> None:
        if plan_id is None:
            return
        session = plan_store.get(plan_id)
        if session is None:
            raise NotFound("계획을 찾을 수 없습니다")
        if container_id is not None and container_id not in {
            c.id for c in session.scenario.containers
        }:
            raise ColdchainError(f"계획에 없는 컨테이너입니다: {container_id}")

    def _record(self, container_id: str, plan_id: str | None = None) -> ContainerRecord:
        key = (plan_id, container_id)
        record = self._records.get(key)
        if record is None:
            record = ContainerRecord(container_id=container_id, plan_id=plan_id)
            self._records[key] = record
        return record

    def _trim(self) -> None:
        while len(self._records) > MAX_CONTAINERS:
            self._records.popitem(last=False)

    def bind(self, body: BindingRequest) -> Binding:
        with self._lock:
            plan_id = self._resolve_plan(body.container_id, body.plan_id)
            self.require_plan(plan_id, body.container_id)
            binding = Binding(
                device_id=body.device_id,
                container_id=body.container_id,
                plan_id=plan_id,
                stage=body.stage,
                bound_at=self.clock(),
            )
            self._bindings[body.device_id] = binding
            record = self._record(body.container_id, plan_id)
            if body.stage:
                record.stage = body.stage
            self._trim()
            return binding

    def bindings(self) -> list[Binding]:
        with self._lock:
            return sorted(self._bindings.values(), key=lambda b: b.device_id)

    def _resolve_plan(self, container_id: str, plan_id: str | None) -> str | None:
        if plan_id is not None:
            return plan_id
        matches = [r for r in self._records.values() if r.container_id == container_id]
        if len(matches) > 1:
            raise ColdchainError("동일 컨테이너의 계획이 여러 개입니다. plan_id를 지정하세요")
        return matches[0].plan_id if matches else None

    def _context(self, record: ContainerRecord, plan_id: str | None = None) -> TwinContext:
        plan_id = plan_id or record.plan_id
        session = self._session(plan_id)
        return build_context(
            record.container_id,
            record.stage,
            self.params,
            plan_id=plan_id if session else None,
            scenario=session.scenario if session else None,
            plan=session.plan if session else None,
        )

    def _evaluate(
        self, record: ContainerRecord, plan_id: str | None = None
    ) -> tuple[TwinContext, Evaluation]:
        ctx = self._context(record, plan_id)
        return ctx, evaluate(
            record.sorted_readings(), ctx, self.params, record.carry, record.anchors
        )

    def _compact(self, record: ContainerRecord) -> list[Alert]:
        excess = len(record.readings) - MAX_READINGS
        if excess <= 0:
            return []
        old = record.sorted_readings()[:excess]
        ev = evaluate(old, self._context(record), self.params, record.carry, record.anchors)
        new = self._merge(record, ev)
        record.carry = ev.carry
        record.history = downsample([*record.history, *ev.timeline], MAX_HISTORY)
        for reading in old:
            del record.readings[(reading.device_id, reading.ts)]
        return new

    def _merge(self, record: ContainerRecord, evaluation: Evaluation) -> list[Alert]:
        record.anchors.update(evaluation.anchors)
        new: list[Alert] = []
        for alert in evaluation.alerts:
            if alert.key in record.alerts:
                continue
            self._seq += 1
            now = self.clock()
            action = alert.action.model_copy(update={"created_at": now}) if alert.action else None
            stored = alert.model_copy(
                update={
                    "seq": self._seq,
                    "created_at": now,
                    "action": action,
                    "plan_id": record.plan_id,
                }
            )
            record.alerts[alert.key] = stored
            self._feed.append(stored)
            new.append(stored)
        if len(self._feed) > MAX_ALERTS:
            del self._feed[: len(self._feed) - MAX_ALERTS]
        return new

    def _summary(self, record: ContainerRecord, ctx: TwinContext, ev: Evaluation) -> TwinSummary:
        loss = loss_summary(ctx, ev.exposure, self.params)
        current = ev.current
        return TwinSummary(
            container_id=record.container_id,
            plan_id=ctx.plan_id,
            status=ev.status,
            reading_count=len(record.readings),
            last_ts=current.ts if current else None,
            temp_c=current.temp_c if current else None,
            power_on=current.power_on if current else None,
            cumulative_outage_minutes=ev.outage.cumulative_minutes,
            sensor_loss_rate=loss.sensor_loss_rate,
            predicted_loss_rate=loss.predicted_loss_rate,
            delta_krw=loss.delta_krw,
            open_actions=sum(1 for a in record.alerts.values() if a.action is not None),
        )

    def ingest(self, items: list[ReadingIn]) -> IngestResult:
        accepted = 0
        duplicates = 0
        rejected: list[IngestRejected] = []
        touched: list[tuple[str | None, str]] = []
        new_alerts: list[Alert] = []
        with self._lock:
            for index, item in enumerate(items):
                binding = self._bindings.get(item.device_id)
                container_id = item.container_id or (binding.container_id if binding else None)
                if container_id is None:
                    rejected.append(
                        IngestRejected(
                            index=index,
                            device_id=item.device_id,
                            reason="container_id가 없고 장치 연결도 없습니다",
                        )
                    )
                    continue
                plan_id = item.plan_id
                if plan_id is None and binding and binding.container_id == container_id:
                    plan_id = binding.plan_id
                try:
                    plan_id = self._resolve_plan(container_id, plan_id)
                    self.require_plan(plan_id, container_id)
                except (NotFound, ColdchainError) as exc:
                    rejected.append(
                        IngestRejected(index=index, device_id=item.device_id, reason=str(exc))
                    )
                    continue
                record = self._record(container_id, plan_id)
                stage = item.stage or (binding.stage if binding else None)
                if stage:
                    record.stage = stage
                reading = Reading(
                    device_id=item.device_id,
                    container_id=container_id,
                    ts=item.ts or self.clock(),
                    temp_c=item.temp_c,
                    power_on=item.power_on,
                    humidity_pct=item.humidity_pct,
                    door_open=item.door_open,
                )
                key = (reading.device_id, reading.ts)
                if key in record.readings:
                    duplicates += 1
                    continue
                compacted = record.carry.prev if record.carry else None
                if compacted is not None and reading.ts <= compacted.ts:
                    rejected.append(
                        IngestRejected(
                            index=index,
                            device_id=item.device_id,
                            reason=f"이미 요약된 구간({compacted.ts.isoformat()}) 이전 측정값입니다",
                        )
                    )
                    continue
                record.readings[key] = reading
                new_alerts.extend(self._compact(record))
                accepted += 1
                record_key = (plan_id, container_id)
                if record_key not in touched:
                    touched.append(record_key)
            summaries: list[TwinSummary] = []
            for record_key in touched:
                record = self._records[record_key]
                ctx, ev = self._evaluate(record)
                new_alerts.extend(self._merge(record, ev))
                summaries.append(self._summary(record, ctx, ev))
            self._trim()
        return IngestResult(
            accepted=accepted,
            duplicates=duplicates,
            rejected=rejected,
            containers=summaries,
            new_alerts=new_alerts,
        )

    def get_record(self, container_id: str, plan_id: str | None = None) -> ContainerRecord:
        with self._lock:
            matches = [
                r
                for r in self._records.values()
                if r.container_id == container_id and (plan_id is None or r.plan_id == plan_id)
            ]
            if not matches:
                raise NotFound(f"센서 기록이 없는 컨테이너입니다: {container_id}")
            if len(matches) > 1:
                raise ColdchainError("동일 컨테이너의 계획이 여러 개입니다. plan_id를 지정하세요")
            return matches[0]

    def state(self, container_id: str, plan_id: str | None = None, points: int = 720) -> TwinState:
        self.require_plan(plan_id, container_id)
        with self._lock:
            record = self.get_record(container_id, plan_id)
            ctx, ev = self._evaluate(record, plan_id)
            alerts = list(record.alerts.values())
            return TwinState(
                container_id=container_id,
                plan_id=ctx.plan_id,
                scenario_id=ctx.scenario_id,
                voyage_id=ctx.voyage.id if ctx.voyage else None,
                stage=record.stage,
                device_ids=sorted({r.device_id for r in record.readings.values()}),
                products=ctx.products,
                status=ev.status,
                current=ev.current,
                outage=ev.outage,
                excursion=ev.excursion,
                loss=loss_summary(ctx, ev.exposure, self.params),
                reading_count=len(record.readings),
                timeline=downsample([*record.history, *ev.timeline], points),
                actions=[a.action for a in alerts if a.action is not None],
                alerts=alerts,
                params=dict(self.params),
            )

    def summaries(self, plan_id: str | None = None) -> list[TwinSummary]:
        with self._lock:
            out = []
            for record in self._records.values():
                if plan_id is not None and record.plan_id != plan_id:
                    continue
                ctx, ev = self._evaluate(record)
                out.append(self._summary(record, ctx, ev))
            return out

    def reset_container(self, container_id: str, plan_id: str | None = None) -> None:
        with self._lock:
            matches = [
                key
                for key in self._records
                if key[1] == container_id and (plan_id is None or key[0] == plan_id)
            ]
            if len(matches) > 1:
                raise ColdchainError("동일 컨테이너의 계획이 여러 개입니다. plan_id를 지정하세요")
            resolved = matches[0][0] if matches else plan_id
            for key in matches:
                self._records.pop(key, None)
            self._feed = [
                a
                for a in self._feed
                if not (a.container_id == container_id and a.plan_id == resolved)
            ]
            for run in self._runs.values():
                if run.info.container_id == container_id and run.info.plan_id == resolved:
                    run.stop.set()

    def alerts(
        self,
        since: int = 0,
        container_id: str | None = None,
        limit: int = 100,
        plan_id: str | None = None,
    ) -> AlertFeed:
        with self._lock:
            items = [
                a
                for a in self._feed
                if a.seq > since
                and (container_id is None or a.container_id == container_id)
                and (plan_id is None or a.plan_id == plan_id)
            ][:limit]
            next_since = items[-1].seq if items else max(since, 0)
            if not items and since > self._seq:
                next_since = self._seq
            return AlertFeed(alerts=items, next_since=next_since)

    def settlement_preview(
        self, container_id: str, plan_id: str | None = None
    ) -> SettlementPreview:
        self.require_plan(plan_id, container_id)
        with self._lock:
            record = self.get_record(container_id, plan_id)
            plan_id = plan_id or record.plan_id
            session = self._session(plan_id)
            if session is None or plan_id is None:
                raise ColdchainError("계획과 연결되지 않은 컨테이너입니다. plan_id를 지정하세요")
            ctx, ev = self._evaluate(record, plan_id)
            loss = loss_summary(ctx, ev.exposure, self.params)
        inspections = list(session.arrival_inspections)
        lots: list[SettlementPreviewLot] = []
        for item in loss.lots:
            try:
                result, inspections = arrival_settlement(
                    plan_id,
                    session.scenario,
                    session.plan,
                    inspections,
                    ArrivalRequest(
                        lot_id=item.lot_id,
                        inspected_loss_pct=min(100.0, item.predicted_loss_rate * 100),
                        note="손실 트윈 예상치",
                    ),
                    self.clock(),
                )
            except QualityError:
                continue
            lots.append(
                SettlementPreviewLot(
                    lot_id=item.lot_id,
                    shipper_id=item.shipper_id,
                    planned_loss_rate=result.planned_loss_rate,
                    predicted_loss_rate=result.inspected_loss_rate,
                    excess_loss_value=result.excess_loss_value,
                    refund_before=result.refund_before,
                    refund_after=result.refund_after,
                    refund_change=result.refund_change,
                    capped=result.capped,
                )
            )
        return SettlementPreview(
            plan_id=plan_id,
            container_id=container_id,
            lots=lots,
            total_refund_change=sum(x.refund_change for x in lots),
            rule=PREVIEW_RULE,
            note="도착 검수 전 예상치. 계획 손실 가정을 넘는 손실액만큼 성과보수에서 뺀다(하한 0)",
        )

    def simulate(self, body: SimulationRequest) -> SimulationRun:
        preset = PRESETS[body.preset]
        with self._lock:
            plan_id = self._resolve_plan(body.container_id, body.plan_id)
            self.require_plan(plan_id, body.container_id)
            if body.reset:
                self.reset_container(body.container_id, plan_id)
            record = self._record(body.container_id, plan_id)
            record.stage = body.stage
            ctx = self._context(record)
            self._trim()
        readings = preset_readings(
            preset,
            device_id=body.device_id,
            container_id=body.container_id,
            plan_id=plan_id,
            band_min_c=ctx.band_min_c,
            band_max_c=ctx.band_max_c,
            start=body.start,
        )
        info = SimulationRun(
            run_id=uuid.uuid4().hex[:12],
            preset=body.preset,
            container_id=body.container_id,
            plan_id=plan_id,
            device_id=body.device_id,
            mode="stream" if body.interval_seconds > 0 else "instant",
            total=len(readings),
            sent=0,
            interval_seconds=body.interval_seconds,
            done=False,
            start=readings[0].ts,
            end=readings[-1].ts,
        )
        if body.interval_seconds <= 0:
            result = self.ingest(readings)
            info.sent = len(readings)
            info.done = True
            info.state = result.containers[0] if result.containers else None
            return info
        run = _Run(info=info, readings=readings, stop=threading.Event())
        run.thread = threading.Thread(target=self._stream, args=(run,), daemon=True)
        with self._lock:
            self._runs[info.run_id] = run
        run.thread.start()
        return info.model_copy()

    def _stream(self, run: _Run) -> None:
        for reading in run.readings:
            with self._lock:
                if run.stop.is_set():
                    break
                result = self.ingest([reading])
                run.info.sent += 1
                if result.containers:
                    run.info.state = result.containers[0]
            if run.stop.wait(run.info.interval_seconds):
                break
        with self._lock:
            run.info.done = True

    def run(self, run_id: str) -> SimulationRun:
        with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                raise NotFound("시뮬레이션 실행을 찾을 수 없습니다")
            return run.info.model_copy()

    def stop(self, run_id: str) -> SimulationRun:
        with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                raise NotFound("시뮬레이션 실행을 찾을 수 없습니다")
            run.stop.set()
        if run.thread is not None:
            run.thread.join(timeout=5)
        return self.run(run_id)

    def wait(self, run_id: str, timeout: float = 30) -> SimulationRun:
        with self._lock:
            run = self._runs.get(run_id)
        if run is None:
            raise NotFound("시뮬레이션 실행을 찾을 수 없습니다")
        if run.thread is not None:
            run.thread.join(timeout=timeout)
        return self.run(run_id)


coldchain_store = ColdchainStore()
