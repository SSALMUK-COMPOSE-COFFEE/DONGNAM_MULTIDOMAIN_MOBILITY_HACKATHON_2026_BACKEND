import secrets
from typing import Annotated

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from freshswitch_backend.config import get_settings

bearer = HTTPBearer(auto_error=False)


def require_demo_reset(
    credential: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> None:
    expected = get_settings().demo_reset_token.get_secret_value()
    if not expected:
        raise HTTPException(status_code=403, detail="관리자 초기화 기능이 비활성화되어 있습니다")
    if credential is None or not secrets.compare_digest(
        expected.encode("utf-8"), credential.credentials.encode("utf-8")
    ):
        raise HTTPException(
            status_code=401,
            detail="관리자 인증이 필요합니다",
            headers={"WWW-Authenticate": "Bearer"},
        )
