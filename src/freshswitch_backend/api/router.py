from fastapi import APIRouter, Depends

from freshswitch_backend.api import (
    analysis,
    auth,
    coldchain,
    compare,
    demo,
    forecast,
    health,
    intake,
    login_gate,
    lots,
    me,
    ops,
    orgs,
    plans,
    provenance,
    quality,
    saas_catalog,
    saas_devices,
    saas_forwarder,
    saas_platform,
    saas_shipper,
    scenarios,
    settlement,
    showcase,
    vessels,
)

api_router = APIRouter(dependencies=[Depends(auth.csrf_guard), Depends(login_gate.require_login)])
api_router.include_router(health.router)
api_router.include_router(scenarios.router)
api_router.include_router(plans.router)
api_router.include_router(forecast.router)
api_router.include_router(vessels.router)
api_router.include_router(compare.router)
api_router.include_router(demo.router)
api_router.include_router(analysis.router)
api_router.include_router(intake.router)
api_router.include_router(lots.router)
api_router.include_router(settlement.router)
api_router.include_router(coldchain.router)
api_router.include_router(quality.router)
api_router.include_router(provenance.router)
api_router.include_router(auth.router)
api_router.include_router(me.router)
api_router.include_router(saas_catalog.router)
api_router.include_router(saas_shipper.router)
api_router.include_router(saas_platform.router)
api_router.include_router(saas_forwarder.router)
api_router.include_router(orgs.router)
api_router.include_router(saas_devices.router)
api_router.include_router(showcase.router)
api_router.include_router(ops.router)
