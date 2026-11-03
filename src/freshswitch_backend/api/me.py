from fastapi import APIRouter

from freshswitch_backend.api.auth import AuthPrincipal, Me, load_me

router = APIRouter(tags=["auth"])


@router.get("/me")
def read_me(principal: AuthPrincipal) -> Me:
    return load_me(principal)
