import asyncio
import csv
import io
import json
import re
import time
from collections.abc import AsyncIterator, Iterable
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response, StreamingResponse
from sqlalchemy import select

from freshswitch_backend.api.auth import (
    OptionalPrincipal,
    Principal,
    app_settings,
    current_org_id,
    current_principal,
)
from freshswitch_backend.api.saas_access import plan_for
from freshswitch_backend.coldchain import (
    ActionConflict,
    ColdchainError,
    DeviceDenied,
    NotFound,
    coldchain_store,
)
from freshswitch_backend.coldchain.devices import device_store, is_simulator_device
from freshswitch_backend.coldchain.models import (
    ActionHandler,
    ActionUpdateBody,
    AlertFeed,
    Binding,
    BindingRequest,
    ColdchainAction,
    IngestResult,
    PowerPlan,
    PresetInfo,
    ReadingIn,
    SettlementPreview,
    SimulationRequest,
    SimulationRun,
    TwinState,
    TwinSummary,
)
from freshswitch_backend.coldchain.power_plan import power_plan
from freshswitch_backend.coldchain.simulator import PRESETS
from freshswitch_backend.coldchain.store import ActionTransition
from freshswitch_backend.domain.models import Quantity
from freshswitch_backend.persistence import database
from freshswitch_backend.persistence.orgs import org_sandbox
from freshswitch_backend.persistence.tables import DEMO_ORG, ShipperProfileRow
from freshswitch_backend.planning.store import store as plan_store

DEVICE_TOKEN_HEADER = "X-Device-Token"
STREAM_RECHECK_SECONDS = 5.0
UNREGISTERED_DEVICE = "등록되지 않은 장치입니다. 조직 기기로 먼저 등록하세요"
REGISTERED_DEVICE_REQUIRED = "조직에 등록된 장치와 X-Device-Token이 필요합니다"
SIMULATOR_SANDBOX_ONLY = "시뮬레이터는 데모 세계(sandbox) 조직에서만 쓸 수 있습니다"
ACTION_ROLE_FORBIDDEN = "콜드체인 조치의 수락·완료는 플랫폼이나 배정된 포워더가 기록합니다"
CSV_MEDIA = "text/csv; charset=utf-8"
CSV_RESPONSE = {200: {"content": {"text/csv": {"schema": {"type": "string"}}}}}

router = APIRouter(prefix="/coldchain", tags=["coldchain"])
OrgId = Annotated[str, Depends(current_org_id)]


def _http(exc: Exception) -> HTTPException:
    if isinstance(exc, NotFound):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, DeviceDenied):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, ActionConflict):
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(status_code=422, detail=str(exc))


class ReadScope:
    def __init__(self, org_id: str, principal: Principal | None) -> None:
        self.org_id = org_id
        self.org_type = principal.org_type if principal is not None else None
        self.shipper_ids: frozenset[str] = frozenset()
        if self.org_type == "shipper" and principal is not None:
            with database.begin() as db:
                self.shipper_ids = frozenset(
                    db.scalars(
                        select(ShipperProfileRow.id).where(
                            ShipperProfileRow.org_id == principal.org_id
                        )
                    )
                )

    @property
    def hides_money(self) -> bool:
        return self.org_type == "forwarder"

    def state(self, state: TwinState) -> TwinState:
        if self.hides_money:
            loss = state.loss.model_copy(
                update={
                    "cargo_value": None,
                    "planned_loss_krw": None,
                    "predicted_loss_krw": None,
                    "delta_krw": None,
                    "lots": [],
                    "shippers": [],
                }
            )
            return state.model_copy(update={"loss": loss})
        if self.org_type != "shipper":
            return state
        lots = [lot for lot in state.loss.lots if lot.shipper_id in self.shipper_ids]
        shippers = [s for s in state.loss.shippers if s.shipper_id in self.shipper_ids]
        loss = state.loss.model_copy(
            update={
                "lots": lots,
                "shippers": shippers,
                "cargo_value": _total(lot.lot_value for lot in lots),
                "planned_loss_krw": _total(lot.planned_loss_krw for lot in lots),
                "predicted_loss_krw": _total(lot.predicted_loss_krw for lot in lots),
                "delta_krw": _total(lot.delta_krw for lot in lots),
            }
        )
        return state.model_copy(update={"loss": loss})

    def summary(self, item: TwinSummary) -> TwinSummary:
        if self.hides_money:
            return item.model_copy(update={"delta_krw": None})
        if self.org_type != "shipper":
            return item
        state = self.state(coldchain_store.state(item.container_id, item.plan_id, 0, self.org_id))
        return item.model_copy(update={"delta_krw": state.loss.delta_krw})

    def preview(self, preview: SettlementPreview) -> SettlementPreview:
        if self.org_type != "shipper":
            return preview
        lots = [lot for lot in preview.lots if lot.shipper_id in self.shipper_ids]
        return preview.model_copy(
            update={"lots": lots, "total_refund_change": sum(x.refund_change for x in lots)}
        )


def _total(values: Iterable[int]) -> int | None:
    items = list(values)
    return sum(items) if items else None


def read_scope(principal: OptionalPrincipal, plan_id: str | None = None) -> ReadScope:
    if principal is None:
        return ReadScope(DEMO_ORG, None)
    if plan_id is None:
        return ReadScope(principal.org_id, principal)
    return ReadScope(plan_for(plan_id, principal).owner_org_id, principal)


Scope = Annotated[ReadScope, Depends(read_scope)]


def _device_org(device_id: str, org_id: str, principal: Principal | None) -> None:
    owner = device_store.owners([device_id]).get(device_id)
    if owner is None:
        if principal is not None:
            raise HTTPException(status_code=422, detail=UNREGISTERED_DEVICE)
        return
    if owner != org_id:
        raise HTTPException(status_code=403, detail=f"다른 조직에 등록된 장치입니다: {device_id}")


def _ingest_org(
    items: list[ReadingIn],
    token: str | None,
    principal: Principal | None,
    saas_mode: bool = False,
) -> str:
    device_ids = {item.device_id for item in items}
    owners = device_store.owners(device_ids)
    if not owners:
        if principal is not None:
            raise HTTPException(status_code=422, detail=UNREGISTERED_DEVICE)
        if saas_mode:
            raise HTTPException(status_code=401, detail=REGISTERED_DEVICE_REQUIRED)
        return DEMO_ORG
    if not token:
        raise HTTPException(status_code=401, detail="등록된 장치는 X-Device-Token이 필요합니다")
    for device_id in sorted(owners):
        if not device_store.verify(device_id, token):
            raise HTTPException(
                status_code=401, detail=f"장치 토큰이 올바르지 않습니다: {device_id}"
            )
    orgs = set(owners.values())
    if len(orgs) > 1 or device_ids - owners.keys():
        raise HTTPException(
            status_code=422, detail="소속 조직이 다른 장치의 측정값을 함께 보낼 수 없습니다"
        )
    return orgs.pop()


@router.get("/params")
def read_params() -> dict[str, Quantity]:
    return dict(coldchain_store.params)


@router.post("/bindings", status_code=status.HTTP_201_CREATED)
def bind(body: BindingRequest, org_id: OrgId, principal: OptionalPrincipal) -> Binding:
    _device_org(body.device_id, org_id, principal)
    try:
        return coldchain_store.bind(body, org_id)
    except (NotFound, ColdchainError) as exc:
        raise _http(exc) from exc


@router.get("/bindings")
def list_bindings(org_id: OrgId) -> list[Binding]:
    return coldchain_store.bindings(org_id)


@router.post("/readings")
def ingest(
    body: list[ReadingIn] | ReadingIn,
    request: Request,
    principal: OptionalPrincipal,
    device_token: Annotated[str | None, Header(alias=DEVICE_TOKEN_HEADER)] = None,
) -> IngestResult:
    items = body if isinstance(body, list) else [body]
    if not items:
        raise HTTPException(status_code=422, detail="측정값이 없습니다")
    if len(items) > 2000:
        raise HTTPException(status_code=422, detail="한 번에 2,000건까지 보낼 수 있습니다")
    org_id = _ingest_org(items, device_token, principal, app_settings(request).saas_mode)
    result = coldchain_store.ingest(items, org_id, bound_only=org_id != DEMO_ORG)
    if org_id != DEMO_ORG:
        device_store.touch({item.device_id for item in items})
    if result.accepted == 0 and result.duplicates == 0:
        raise HTTPException(status_code=422, detail=result.rejected[0].reason)
    return result


@router.get("/containers")
def list_containers(scope: Scope, plan_id: str | None = None) -> list[TwinSummary]:
    return [scope.summary(item) for item in coldchain_store.summaries(plan_id, scope.org_id)]


@router.get("/containers/{container_id}")
def read_twin(
    container_id: str,
    scope: Scope,
    plan_id: str | None = None,
    points: int = Query(720, ge=0, le=5000),
) -> TwinState:
    try:
        return scope.state(coldchain_store.state(container_id, plan_id, points, scope.org_id))
    except (NotFound, ColdchainError) as exc:
        raise _http(exc) from exc


@router.delete("/containers/{container_id}", status_code=status.HTTP_204_NO_CONTENT)
def reset_container(container_id: str, org_id: OrgId, plan_id: str | None = None) -> None:
    try:
        coldchain_store.reset_container(container_id, plan_id, org_id)
    except ColdchainError as exc:
        raise _http(exc) from exc


@router.get("/containers/{container_id}/settlement-preview")
def settlement_preview(
    container_id: str, scope: Scope, plan_id: str | None = None
) -> SettlementPreview:
    if scope.hides_money:
        raise HTTPException(status_code=403, detail="포워더는 정산 금액을 볼 수 없습니다")
    try:
        return scope.preview(
            coldchain_store.settlement_preview(container_id, plan_id, scope.org_id)
        )
    except (NotFound, ColdchainError) as exc:
        raise _http(exc) from exc


def _handler(principal: Principal | None) -> ActionHandler:
    if principal is None:
        return ActionHandler(org_type="platform", org_id=DEMO_ORG)
    if principal.org_type not in ("platform", "forwarder"):
        raise HTTPException(status_code=403, detail=ACTION_ROLE_FORBIDDEN)
    return ActionHandler(
        org_type=principal.org_type, org_id=principal.org_id, user_id=principal.user_id
    )


def _update_action(
    container_id: str,
    action_key: str,
    transition: ActionTransition,
    scope: ReadScope,
    principal: Principal | None,
    body: ActionUpdateBody | None,
    plan_id: str | None,
) -> ColdchainAction:
    handler = _handler(principal)
    try:
        return coldchain_store.update_action(
            container_id,
            action_key,
            transition,
            handler,
            body.note if body else None,
            plan_id,
            scope.org_id,
        )
    except (NotFound, ColdchainError) as exc:
        raise _http(exc) from exc


@router.post("/containers/{container_id}/actions/{action_key}/accept")
def accept_action(
    container_id: str,
    action_key: str,
    scope: Scope,
    principal: OptionalPrincipal,
    body: ActionUpdateBody | None = None,
    plan_id: str | None = None,
) -> ColdchainAction:
    return _update_action(container_id, action_key, "accept", scope, principal, body, plan_id)


@router.post("/containers/{container_id}/actions/{action_key}/complete")
def complete_action(
    container_id: str,
    action_key: str,
    scope: Scope,
    principal: OptionalPrincipal,
    body: ActionUpdateBody | None = None,
    plan_id: str | None = None,
) -> ColdchainAction:
    return _update_action(container_id, action_key, "complete", scope, principal, body, plan_id)


def _require_participation(scope: ReadScope, container_id: str, plan_id: str | None) -> None:
    if scope.org_type != "shipper":
        return
    state = coldchain_store.state(container_id, plan_id, 0, scope.org_id)
    if not any(lot.shipper_id in scope.shipper_ids for lot in state.loss.lots):
        raise NotFound(f"센서 기록이 없는 컨테이너입니다: {container_id}")


def _csv(container_id: str, kind: str, header: list[str], rows: list[list]) -> Response:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows)
    name = re.sub(r"[^A-Za-z0-9_-]", "_", container_id)
    return Response(
        content=buffer.getvalue(),
        media_type=CSV_MEDIA,
        headers={"Content-Disposition": f'attachment; filename="{name}-{kind}.csv"'},
    )


def _cell(value: object) -> object:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


def _export(scope: ReadScope, container_id: str, plan_id: str | None):
    try:
        _require_participation(scope, container_id, plan_id)
        return coldchain_store.export_points(container_id, plan_id, scope.org_id)
    except (NotFound, ColdchainError) as exc:
        raise _http(exc) from exc


@router.get(
    "/containers/{container_id}/readings.csv",
    response_class=Response,
    responses=CSV_RESPONSE,
)
def export_readings(container_id: str, scope: Scope, plan_id: str | None = None) -> Response:
    readings, history, _ = _export(scope, container_id, plan_id)
    rows = [
        [p.ts.isoformat(), "", p.temp_c, p.power_on, p.humidity_pct, p.door_open, "compacted"]
        for p in history
    ] + [
        [r.ts.isoformat(), r.device_id, r.temp_c, r.power_on, r.humidity_pct, r.door_open, "raw"]
        for r in readings
    ]
    header = ["ts", "device_id", "temp_c", "power_on", "humidity_pct", "door_open", "source"]
    return _csv(container_id, "readings", header, [[_cell(v) for v in row] for row in rows])


@router.get(
    "/containers/{container_id}/outages.csv",
    response_class=Response,
    responses=CSV_RESPONSE,
)
def export_outages(container_id: str, scope: Scope, plan_id: str | None = None) -> Response:
    _, _, episodes = _export(scope, container_id, plan_id)
    rows = [
        [
            index,
            e.start.isoformat(),
            e.end.isoformat() if e.end else None,
            round(e.minutes, 1),
            e.end is None,
            e.start_temp_c,
            e.max_temp_c,
            e.end_temp_c,
        ]
        for index, e in enumerate(episodes, start=1)
    ]
    header = [
        "episode",
        "start",
        "end",
        "minutes",
        "ongoing",
        "start_temp_c",
        "max_temp_c",
        "end_temp_c",
    ]
    return _csv(container_id, "outages", header, [[_cell(v) for v in row] for row in rows])


@router.get("/plans/{plan_id}/power-plan")
def read_power_plan(plan_id: str, principal: OptionalPrincipal) -> PowerPlan:
    if principal is None:
        session = plan_store.get(plan_id, org_id=DEMO_ORG)
        if session is None:
            raise HTTPException(status_code=404, detail="계획을 찾을 수 없습니다")
        return power_plan(session)
    access = plan_for(plan_id, principal)
    shipper_ids = access.viewer.shipper_ids if access.viewer.org_type == "shipper" else None
    return power_plan(access.session, shipper_ids=shipper_ids)


@router.get("/alerts")
def read_alerts(
    scope: Scope,
    since: int = Query(0, ge=0),
    container_id: str | None = None,
    limit: int = Query(100, ge=1, le=1000),
    plan_id: str | None = None,
) -> AlertFeed:
    return coldchain_store.alerts(since, container_id, limit, plan_id, scope.org_id)


def still_allowed(request: Request, principal: Principal | None, plan_id: str | None) -> bool:
    if principal is None:
        return True
    try:
        current = current_principal(request)
        if current is None or (current.user_id, current.org_id) != (
            principal.user_id,
            principal.org_id,
        ):
            return False
        if plan_id is not None:
            plan_for(plan_id, current)
    except HTTPException:
        return False
    return True


@router.get("/alerts/stream")
async def stream_alerts(
    request: Request,
    scope: Scope,
    principal: OptionalPrincipal,
    since: int = Query(0, ge=0),
    container_id: str | None = None,
    timeout: float = Query(300, ge=0, le=3600),
    plan_id: str | None = None,
) -> StreamingResponse:
    async def events() -> AsyncIterator[str]:
        cursor = since
        deadline = time.monotonic() + timeout
        recheck_at = time.monotonic() + STREAM_RECHECK_SECONDS
        while True:
            if principal is not None and time.monotonic() >= recheck_at:
                recheck_at = time.monotonic() + STREAM_RECHECK_SECONDS
                if not await run_in_threadpool(still_allowed, request, principal, plan_id):
                    break
            feed = await run_in_threadpool(
                coldchain_store.alerts, cursor, container_id, 1000, plan_id, scope.org_id
            )
            for alert in feed.alerts:
                data = json.dumps(alert.model_dump(mode="json"), ensure_ascii=False)
                yield f"id: {alert.seq}\nevent: alert\ndata: {data}\n\n"
            cursor = feed.next_since
            if time.monotonic() >= deadline:
                break
            yield ": keepalive\n\n"
            await asyncio.sleep(1)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )


@router.get("/simulate/presets")
def list_presets() -> list[PresetInfo]:
    return [p.info() for p in PRESETS.values()]


@router.post("/simulate")
def simulate(body: SimulationRequest, org_id: OrgId, principal: OptionalPrincipal) -> SimulationRun:
    if principal is not None and not org_sandbox(principal.org_id):
        raise HTTPException(status_code=403, detail=SIMULATOR_SANDBOX_ONLY)
    if not is_simulator_device(body.device_id):
        _device_org(body.device_id, org_id, principal)
    try:
        return coldchain_store.simulate(body, org_id)
    except (NotFound, ColdchainError) as exc:
        raise _http(exc) from exc


@router.get("/simulate/{run_id}")
def read_run(run_id: str, org_id: OrgId) -> SimulationRun:
    try:
        return coldchain_store.run(run_id, org_id)
    except NotFound as exc:
        raise _http(exc) from exc


@router.delete("/simulate/{run_id}")
def stop_run(run_id: str, org_id: OrgId) -> SimulationRun:
    try:
        return coldchain_store.stop(run_id, org_id)
    except NotFound as exc:
        raise _http(exc) from exc
