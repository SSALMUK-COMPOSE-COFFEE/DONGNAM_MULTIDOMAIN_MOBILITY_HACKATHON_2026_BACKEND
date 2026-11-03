"""Public error descriptions must never contain request URLs or upstream bodies."""

import re

import httpx


def public_error(exc: BaseException, service: str) -> str:
    if isinstance(exc, (TimeoutError, httpx.TimeoutException)):
        return f"{service} 응답 시간 초과"
    if isinstance(exc, httpx.HTTPStatusError):
        return f"{service} HTTP 오류 [{exc.response.status_code}]"
    if isinstance(exc, httpx.HTTPError):
        return f"{service} 연결 오류"
    code = getattr(exc, "code", None)
    if isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,10}", code):
        return f"{service} 오류 [{code}]"
    return f"{service} 응답 처리 오류"
