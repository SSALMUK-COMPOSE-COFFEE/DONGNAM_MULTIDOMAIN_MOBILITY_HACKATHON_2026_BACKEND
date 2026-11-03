from fastapi import APIRouter, HTTPException, status

from freshswitch_backend.api.auth import AuthPrincipal
from freshswitch_backend.coldchain.devices import (
    Device,
    DeviceConflict,
    DeviceRequest,
    RegisteredDevice,
    device_store,
)

router = APIRouter(prefix="/saas/devices", tags=["saas-devices"])


@router.get("")
def list_devices(principal: AuthPrincipal) -> list[Device]:
    return device_store.for_org(principal.org_id)


@router.post("", status_code=status.HTTP_201_CREATED)
def register_device(body: DeviceRequest, principal: AuthPrincipal) -> RegisteredDevice:
    try:
        return device_store.register(body, principal.org_id)
    except DeviceConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.delete("/{device_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_device(device_id: str, principal: AuthPrincipal) -> None:
    if not device_store.delete(device_id, principal.org_id):
        raise HTTPException(status_code=404, detail="장치를 찾을 수 없습니다")
