from fastapi import APIRouter, HTTPException

from freshswitch_backend.data.provenance import provenance_catalog

router = APIRouter(prefix="/data", tags=["data"])


@router.get("/provenance")
def read_provenance(scenario_id: str = "default") -> dict:
    try:
        return provenance_catalog(scenario_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="시나리오를 찾을 수 없습니다") from exc
