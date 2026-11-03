from dataclasses import dataclass
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from freshswitch_backend.api.auth import OptionalPrincipal, Principal
from freshswitch_backend.api.saas_access import plan_for
from freshswitch_backend.data.loader import get_scenario
from freshswitch_backend.domain.models import Plan, Scenario
from freshswitch_backend.intake.models import IntakePreview, IntakeRequest
from freshswitch_backend.intake.savings import estimate_savings
from freshswitch_backend.intake.service import IntakeError, preview_lot
from freshswitch_backend.intake.tenants import load_catalog, tenant_scenario
from freshswitch_backend.lots import ANY_ORG, LotConflict, Reservation, lot_store
from freshswitch_backend.persistence.tables import DEMO_ORG
from freshswitch_backend.planning.store import store

router = APIRouter(prefix="/intake", tags=["intake"])


@dataclass(frozen=True)
class IntakeScope:
    org_id: str = DEMO_ORG
    catalog_org: str | None = None
    lot_org: str | None = DEMO_ORG
    peer_of: str | None = None
    shipper: Principal | None = None

    def scenario(self, scenario_id: str) -> Scenario | None:
        base = get_scenario(scenario_id)
        if base is None or self.catalog_org is None:
            return base
        return tenant_scenario(base, load_catalog(self.catalog_org, base, self.peer_of))

    def lot_org_for(self, shipper_id: str) -> str:
        if self.lot_org is not None:
            return self.lot_org
        org = load_catalog(self.catalog_org or ANY_ORG, peer_of=self.peer_of).org_of(shipper_id)
        if org is None:
            raise HTTPException(
                status_code=422, detail=f"소속 조직이 없는 화주입니다: {shipper_id}"
            )
        return org


DEMO_SCOPE = IntakeScope()


def intake_scope(principal: OptionalPrincipal) -> IntakeScope:
    if principal is None:
        return DEMO_SCOPE
    if principal.org_type == "platform":
        return IntakeScope(
            org_id=principal.org_id,
            catalog_org=ANY_ORG,
            lot_org=None,
            peer_of=principal.org_id,
        )
    if principal.org_type == "shipper":
        return IntakeScope(
            org_id=principal.org_id,
            catalog_org=principal.org_id,
            lot_org=principal.org_id,
            shipper=principal,
        )
    raise HTTPException(status_code=403, detail="이 조직 유형은 출하를 등록할 수 없습니다")


Scope = Annotated[IntakeScope, Depends(intake_scope)]


def intake_error(exc: IntakeError) -> HTTPException:
    return HTTPException(status_code=422, detail=[e.model_dump() for e in exc.errors])


def _shipper_plan(plan_id: str, principal: Principal) -> tuple[Scenario, Plan]:
    session = plan_for(plan_id, principal).session
    scenario = session.scenario
    catalog = load_catalog(principal.org_id, scenario)
    known = {p.id for p in scenario.packhouses}
    packhouses = [*scenario.packhouses, *(p for p in catalog.packhouses if p.id not in known)]
    own = scenario.model_copy(
        update={"shippers": [s.shipper for s in catalog.shippers], "packhouses": packhouses}
    )
    return own, session.plan


def _plan_scenario(plan_id: str, scope: IntakeScope) -> tuple[Scenario, Plan]:
    if scope.shipper is not None:
        return _shipper_plan(plan_id, scope.shipper)
    session = store.get(plan_id, org_id=scope.org_id)
    if session is None:
        raise HTTPException(status_code=404, detail="계획을 찾을 수 없습니다")
    return session.scenario, session.plan


def build_preview(
    body: IntakeRequest,
    reservation: Reservation | None = None,
    scope: IntakeScope = DEMO_SCOPE,
) -> tuple[IntakePreview, str]:
    plan = None
    if body.plan_id is not None:
        scenario, plan = _plan_scenario(body.plan_id, scope)
    else:
        scenario = scope.scenario(body.scenario_id)
        if scenario is None:
            raise HTTPException(status_code=404, detail="시나리오를 찾을 수 없습니다")
    reservation = reservation or lot_store.reservation(
        scope.lot_org or ANY_ORG, peer_of=scope.peer_of
    )
    try:
        preview = preview_lot(
            scenario,
            body,
            plan,
            taken_ids=reservation.taken,
            reserved_ids=reservation.reserved,
            check_season=scope.org_id != DEMO_ORG,
        )
    except IntakeError as exc:
        raise intake_error(exc) from exc
    estimate = estimate_savings(scenario, preview.lot, plan, preview.hints)
    return preview.model_copy(update={"savings_estimate": estimate}), scenario.id


def register(body: IntakeRequest, scope: IntakeScope = DEMO_SCOPE) -> IntakePreview:
    built: list[IntakePreview] = []
    owner: list[str] = []

    def build(reservation: Reservation):
        preview, scenario_id = build_preview(body, reservation, scope)
        built.append(preview)
        return preview.lot, scenario_id, preview.resolved.packhouse_id

    if scope.lot_org is None:
        preview, _ = build_preview(body, scope=scope)
        owner.append(scope.lot_org_for(preview.lot.shipper_id))
    try:
        registered = lot_store.create(build, org_id=owner[0] if owner else scope.lot_org)
    except LotConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return built[-1].model_copy(
        update={
            "registered": registered,
            "next_step": (
                "등록된 로트는 POST /api/plans 에 lot_ids 또는 include_registered=true 로 넣어 "
                "계획에 포함한다. plan_id가 있으면 event를 POST /api/plans/{plan_id}/events 에 보낸다"
            ),
        }
    )


@router.post("/lots")
def intake_lot(body: IntakeRequest, scope: Scope) -> IntakePreview:
    if body.save:
        return register(body, scope)
    return build_preview(body, scope=scope)[0]
