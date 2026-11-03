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
    lots,
    me,
    orgs,
    plans,
    provenance,
    quality,
    saas_catalog,
    saas_forwarder,
    saas_platform,
    saas_shipper,
    scenarios,
    settlement,
    vessels,
)

api_router = APIRouter(dependencies=[Depends(auth.csrf_guard)])
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
