from fastapi import APIRouter

from freshswitch_backend.config import get_settings

router = APIRouter(tags=["health"])


@router.get("/health")
def health() -> dict:
    settings = get_settings()
    return {
        "status": "ok",
        "data_go_kr_configured": bool(settings.data_go_kr_service_key),
        "llm_configured": bool(settings.llm_api_key and settings.llm_model_name),
    }
