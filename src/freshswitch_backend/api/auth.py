import argparse
import hashlib
import math
import os
import re
import secrets
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from ipaddress import IPv4Network, IPv6Network, ip_address, ip_network
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from freshswitch_backend.config import Settings, get_settings
from freshswitch_backend.persistence import database
from freshswitch_backend.persistence.passwords import hash_password, verify_password
from freshswitch_backend.persistence.seed import demo_orgs, is_demo_email
from freshswitch_backend.persistence.tables import (
    DEMO_ORG,
    AuthSessionRow,
    MembershipRow,
    OrganizationRow,
    UserRow,
)

ORG_HEADER = "X-Org-Id"
CSRF_HEADER = "X-Requested-With"
CSRF_VALUE = "freshswitch"
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
LAST_SEEN_INTERVAL = timedelta(minutes=1)
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
INVALID_LOGIN = "이메일 또는 비밀번호가 올바르지 않습니다"
TOO_MANY_REQUESTS = "요청이 너무 많습니다. 잠시 후 다시 시도하세요"
DEMO_SESSION_CAP = 5000
DEMO_SESSION_TTL = timedelta(hours=12)
TRUSTED_PROXIES_ENV = "TRUSTED_PROXIES"
DEFAULT_TRUSTED_PROXIES = "127.0.0.0/8,::1/128,172.16.0.0/12"

OrgType = Literal["platform", "shipper", "forwarder"]
SignupOrgType = Literal["shipper", "forwarder"]

router = APIRouter(prefix="/auth", tags=["auth"])


@dataclass(frozen=True)
class RateRule:
    per_client: int
    window_seconds: float


AUTH_RATE_RULES: dict[str, RateRule] = {
    "demo-login": RateRule(per_client=60, window_seconds=60),
    "signup": RateRule(per_client=30, window_seconds=600),
}


class SlidingWindowLimiter:
    def __init__(
        self, rules: dict[str, RateRule], clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._rules = rules
        self._clock = clock
        self._hits: dict[tuple[str, str], deque[float]] = {}
        self._lock = threading.Lock()

    def _window(self, key: tuple[str, str], now: float, window: float) -> deque[float]:
        hits = self._hits.setdefault(key, deque())
        while hits and hits[0] <= now - window:
            hits.popleft()
        return hits

    def _sweep(self, now: float) -> None:
        for key in list(self._hits):
            rule = self._rules.get(key[0])
            hits = self._hits[key]
            if rule is None or not hits or hits[-1] <= now - rule.window_seconds:
                del self._hits[key]

    def hit(self, route: str, client: str) -> float | None:
        rule = self._rules[route]
        now = self._clock()
        with self._lock:
            if len(self._hits) > 10_000:
                self._sweep(now)
            hits = self._window((route, client), now, rule.window_seconds)
            if len(hits) >= rule.per_client:
                return max(hits[0] + rule.window_seconds - now, 0.0)
            hits.append(now)
            return None


_limiter_lock = threading.Lock()


@dataclass(frozen=True)
class Principal:
    user_id: str
    org_id: str
    org_type: str
    role: str


class MeUser(BaseModel):
    id: str
    email: str
    name: str


class OrgMembership(BaseModel):
    org_id: str
    org_type: str
    org_name: str
    role: str
    sandbox: bool = False


class Me(BaseModel):
    user: MeUser
    memberships: list[OrgMembership]
    active_org: OrgMembership


def _normalize_email(value: str) -> str:
    email = value.strip().lower()
    if not EMAIL_PATTERN.fullmatch(email):
        raise ValueError("이메일 형식이 올바르지 않습니다")
    return email


class SignupRequest(BaseModel):
    email: str = Field(max_length=320)
    password: str = Field(min_length=8, max_length=128)
    name: str = Field(min_length=1, max_length=200)
    org_name: str = Field(min_length=1, max_length=200)
    org_type: SignupOrgType

    @field_validator("email")
    @classmethod
    def _email(cls, value: str) -> str:
        return _normalize_email(value)

    @field_validator("name", "org_name")
    @classmethod
    def _text(cls, value: str) -> str:
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("빈 값은 사용할 수 없습니다")
        return trimmed


class LoginRequest(BaseModel):
    email: str = Field(max_length=320)
    password: str = Field(max_length=128)

    @field_validator("email")
    @classmethod
    def _email(cls, value: str) -> str:
        return value.strip().lower()


class DemoLoginRequest(BaseModel):
    org_type: OrgType
    org_id: str | None = Field(default=None, max_length=64)


class ActiveOrgRequest(BaseModel):
    org_id: str = Field(min_length=1, max_length=64)


def app_settings(request: Request) -> Settings:
    return getattr(request.app.state, "settings", None) or get_settings()


def _now() -> datetime:
    return datetime.now(UTC)


Network = IPv4Network | IPv6Network


@lru_cache
def _networks(raw: str) -> tuple[Network, ...]:
    return tuple(ip_network(part.strip(), strict=False) for part in raw.split(",") if part.strip())


def trusted_proxies() -> tuple[Network, ...]:
    return _networks(os.environ.get(TRUSTED_PROXIES_ENV, DEFAULT_TRUSTED_PROXIES))


def _is_trusted_proxy(host: str) -> bool:
    try:
        address = ip_address(host)
    except ValueError:
        return False
    return any(address in network for network in trusted_proxies())


def client_address(request: Request) -> str:
    peer = request.client.host if request.client else "unknown"
    if not _is_trusted_proxy(peer):
        return peer
    forwarded = [hop.strip() for hop in request.headers.get("x-forwarded-for", "").split(",")]
    hops = [hop for hop in forwarded if hop]
    if hops:
        return hops[-1]
    real = request.headers.get("x-real-ip", "").strip()
    if real:
        return real
    return peer


def auth_limiter(request: Request) -> SlidingWindowLimiter:
    state = request.app.state
    with _limiter_lock:
        limiter = getattr(state, "auth_rate_limiter", None)
        if limiter is None:
            limiter = SlidingWindowLimiter(AUTH_RATE_RULES)
            state.auth_rate_limiter = limiter
        return limiter


def rate_limit(route: str) -> Callable[[Request], None]:
    def dependency(request: Request) -> None:
        if not app_settings(request).saas_mode:
            return
        retry = auth_limiter(request).hit(route, client_address(request))
        if retry is not None:
            raise HTTPException(
                status_code=429,
                detail=TOO_MANY_REQUESTS,
                headers={"Retry-After": str(max(1, math.ceil(retry)))},
            )

    return dependency


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@lru_cache
def _dummy_hash() -> str:
    return hash_password(secrets.token_urlsafe(16))


def _memberships(db: Session, user_id: str) -> list[OrgMembership]:
    rows = db.execute(
        select(
            OrganizationRow.id,
            OrganizationRow.type,
            OrganizationRow.name,
            MembershipRow.role,
            OrganizationRow.sandbox,
        )
        .join(OrganizationRow, OrganizationRow.id == MembershipRow.org_id)
        .where(MembershipRow.user_id == user_id)
        .order_by(MembershipRow.created_at, MembershipRow.org_id)
    ).all()
    return [
        OrgMembership(org_id=org_id, org_type=kind, org_name=name, role=role, sandbox=sandbox)
        for org_id, kind, name, role, sandbox in rows
    ]


def _choose(
    memberships: list[OrgMembership], requested: str | None, remembered: str | None
) -> OrgMembership:
    if not memberships:
        raise HTTPException(status_code=403, detail="소속된 조직이 없습니다")
    by_id = {m.org_id: m for m in memberships}
    if requested:
        if requested not in by_id:
            raise HTTPException(status_code=403, detail="소속되지 않은 조직입니다")
        return by_id[requested]
    if remembered in by_id:
        return by_id[remembered]
    return memberships[0]


def _me(user: UserRow, memberships: list[OrgMembership], active: OrgMembership) -> Me:
    return Me(
        user=MeUser(id=user.id, email=user.email, name=user.name),
        memberships=memberships,
        active_org=active,
    )


def load_me(principal: Principal) -> Me:
    with database.begin() as db:
        user = db.get(UserRow, principal.user_id)
        if user is None:
            raise HTTPException(status_code=401, detail="로그인이 필요합니다")
        memberships = _memberships(db, user.id)
        active = next((m for m in memberships if m.org_id == principal.org_id), None)
        if active is None:
            raise HTTPException(status_code=403, detail="소속되지 않은 조직입니다")
        return _me(user, memberships, active)


def csrf_guard(request: Request) -> None:
    if request.method not in UNSAFE_METHODS:
        return
    if not request.cookies.get(app_settings(request).session_cookie_name):
        return
    if request.headers.get(CSRF_HEADER) != CSRF_VALUE:
        raise HTTPException(status_code=403, detail="요청 출처를 확인할 수 없습니다")


def _session_required(settings: Settings, *, stale_cookie: bool) -> HTTPException:
    headers: dict[str, str] = {}
    if stale_cookie:
        cleared = Response()
        _clear_cookie(cleared, settings)
        headers["set-cookie"] = cleared.headers["set-cookie"]
    return HTTPException(status_code=401, detail="로그인이 필요합니다", headers=headers or None)


def current_principal(request: Request) -> Principal | None:
    settings = app_settings(request)
    token = request.cookies.get(settings.session_cookie_name)
    requested = request.headers.get(ORG_HEADER) or None
    if not token:
        if requested is not None:
            raise _session_required(settings, stale_cookie=False)
        return None
    now = _now()
    with database.begin() as db:
        session = db.get(AuthSessionRow, _token_hash(token))
        if session is None or session.expires_at <= now:
            raise _session_required(settings, stale_cookie=True)
        active = _choose(_memberships(db, session.user_id), requested, session.active_org_id)
        if requested is None and session.active_org_id != active.org_id:
            session.active_org_id = active.org_id
        if session.last_seen_at is None or now - session.last_seen_at >= LAST_SEEN_INTERVAL:
            session.last_seen_at = now
        return Principal(
            user_id=session.user_id,
            org_id=active.org_id,
            org_type=active.org_type,
            role=active.role,
        )


OptionalPrincipal = Annotated[Principal | None, Depends(current_principal)]


def require_principal(principal: OptionalPrincipal) -> Principal:
    if principal is None:
        raise HTTPException(status_code=401, detail="로그인이 필요합니다")
    return principal


AuthPrincipal = Annotated[Principal, Depends(require_principal)]


def require_role(*org_types: str) -> Callable[[Principal], Principal]:
    allowed = frozenset(org_types)

    def dependency(principal: AuthPrincipal) -> Principal:
        if principal.org_type not in allowed:
            raise HTTPException(status_code=403, detail="이 조직 유형은 사용할 수 없는 기능입니다")
        return principal

    return dependency


def current_org_id(principal: OptionalPrincipal) -> str:
    return principal.org_id if principal is not None else DEMO_ORG


def _set_cookie(response: Response, settings: Settings, token: str, ttl: timedelta) -> None:
    response.set_cookie(
        settings.session_cookie_name,
        token,
        max_age=int(ttl.total_seconds()),
        path=settings.cookie_path,
        secure=settings.session_cookie_secure,
        httponly=True,
        samesite="lax",
    )


def _clear_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        settings.session_cookie_name,
        path=settings.cookie_path,
        secure=settings.session_cookie_secure,
        httponly=True,
        samesite="lax",
    )


def _start_session(
    db: Session,
    request: Request,
    response: Response,
    user: UserRow,
    preferred_org: str | None = None,
) -> Me:
    settings = app_settings(request)
    memberships = _memberships(db, user.id)
    active = _choose(memberships, None, preferred_org)
    now = _now()
    previous = request.cookies.get(settings.session_cookie_name)
    if previous:
        db.execute(delete(AuthSessionRow).where(AuthSessionRow.token_hash == _token_hash(previous)))
    db.execute(
        delete(AuthSessionRow).where(
            AuthSessionRow.user_id == user.id, AuthSessionRow.expires_at <= now
        )
    )
    ttl = timedelta(seconds=settings.session_ttl)
    if user.is_demo:
        ttl = min(ttl, DEMO_SESSION_TTL)
        surplus = (
            select(AuthSessionRow.token_hash)
            .where(AuthSessionRow.user_id == user.id)
            .order_by(
                AuthSessionRow.last_seen_at.desc().nulls_last(),
                AuthSessionRow.created_at.desc(),
                AuthSessionRow.token_hash,
            )
            .offset(DEMO_SESSION_CAP - 1)
        )
        db.execute(delete(AuthSessionRow).where(AuthSessionRow.token_hash.in_(surplus)))
    token = secrets.token_urlsafe(32)
    db.add(
        AuthSessionRow(
            token_hash=_token_hash(token),
            user_id=user.id,
            active_org_id=active.org_id,
            expires_at=now + ttl,
            last_seen_at=now,
        )
    )
    _set_cookie(response, settings, token, ttl)
    return _me(user, memberships, active)


def purge_expired_sessions(now: datetime | None = None) -> int:
    with database.begin() as db:
        result = db.execute(
            delete(AuthSessionRow).where(AuthSessionRow.expires_at <= (now or _now()))
        )
        return result.rowcount or 0


@router.post(
    "/signup",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(rate_limit("signup"))],
)
def signup(body: SignupRequest, request: Request, response: Response) -> Me:
    if is_demo_email(body.email):
        raise HTTPException(status_code=422, detail="데모 계정 도메인으로는 가입할 수 없습니다")
    password_hash = hash_password(body.password)
    try:
        with database.begin() as db:
            exists = db.scalar(select(UserRow.id).where(func.lower(UserRow.email) == body.email))
            if exists is not None:
                raise HTTPException(status_code=409, detail="이미 가입된 이메일입니다")
            org_id = f"{body.org_type}-{uuid.uuid4().hex[:12]}"
            user = UserRow(
                id=f"user-{uuid.uuid4().hex}",
                email=body.email,
                password_hash=password_hash,
                name=body.name,
                is_demo=False,
            )
            db.add(OrganizationRow(id=org_id, type=body.org_type, name=body.org_name))
            db.add(user)
            db.flush()
            db.add(MembershipRow(user_id=user.id, org_id=org_id, role="admin"))
            db.flush()
            return _start_session(db, request, response, user)
    except IntegrityError as exc:
        raise HTTPException(status_code=409, detail="이미 가입된 이메일입니다") from exc


@router.post("/login")
def login(body: LoginRequest, request: Request, response: Response) -> Me:
    with database.begin() as db:
        user = db.scalar(select(UserRow).where(func.lower(UserRow.email) == body.email))
        valid = verify_password(body.password, user.password_hash if user else _dummy_hash())
        if user is None or not valid:
            raise HTTPException(status_code=401, detail=INVALID_LOGIN)
        if app_settings(request).saas_mode and (user.is_demo or is_demo_email(user.email)):
            raise HTTPException(status_code=401, detail=INVALID_LOGIN)
        return _start_session(db, request, response, user)


def _demo_rank() -> dict[str, int]:
    return {org.id: index for index, org in enumerate(demo_orgs())}


@router.post("/demo-login", dependencies=[Depends(rate_limit("demo-login"))])
def demo_login(body: DemoLoginRequest, request: Request, response: Response) -> Me:
    with database.begin() as db:
        candidates = db.execute(
            select(UserRow, OrganizationRow.id)
            .join(MembershipRow, MembershipRow.user_id == UserRow.id)
            .join(OrganizationRow, OrganizationRow.id == MembershipRow.org_id)
            .where(
                UserRow.is_demo.is_(True),
                OrganizationRow.type == body.org_type,
                OrganizationRow.sandbox.is_(True),
            )
        ).all()
        if body.org_id is not None:
            candidates = [row for row in candidates if row[1] == body.org_id]
        if not candidates:
            raise HTTPException(status_code=404, detail="데모 계정이 준비되어 있지 않습니다")
        rank = _demo_rank()
        user, org_id = min(candidates, key=lambda row: (rank.get(row[1], len(rank)), row[1]))
        return _start_session(db, request, response, user, org_id)


@router.put("/active-org")
def switch_active_org(body: ActiveOrgRequest, request: Request, _: AuthPrincipal) -> Me:
    settings = app_settings(request)
    token = request.cookies.get(settings.session_cookie_name)
    if not token:
        raise _session_required(settings, stale_cookie=False)
    with database.begin() as db:
        session = db.get(AuthSessionRow, _token_hash(token))
        if session is None or session.expires_at <= _now():
            raise _session_required(settings, stale_cookie=True)
        user = db.get(UserRow, session.user_id)
        if user is None:
            raise _session_required(settings, stale_cookie=True)
        memberships = _memberships(db, user.id)
        active = _choose(memberships, body.org_id, None)
        session.active_org_id = active.org_id
        return _me(user, memberships, active)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(request: Request) -> Response:
    settings = app_settings(request)
    token = request.cookies.get(settings.session_cookie_name)
    if token:
        with database.begin() as db:
            db.execute(
                delete(AuthSessionRow).where(AuthSessionRow.token_hash == _token_hash(token))
            )
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    _clear_cookie(response, settings)
    return response


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="freshswitch_backend.api.auth")
    parser.add_subparsers(dest="command", required=True).add_parser("purge-sessions")
    parser.parse_args(argv)
    print(f"purged={purge_expired_sessions()}")


if __name__ == "__main__":
    main()
