"""Sign-in, first-run setup, BYheaven single sign-on (ADR-0020).

The gate (`api/gate.py`) lets exactly these through unauthenticated — `status`,
`login`, `setup`, `sso`, `logout` — and the health probe. Everything else under
`/api` needs the cookie these routes set.

The session is an `HttpOnly`, `SameSite=Strict` cookie rather than a bearer
token the page holds: script cannot read it, and the graph WebSocket carries it
on the handshake with nothing to mint and nothing to put in a URL.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from bystack.api.deps import Context
from bystack.api.gate import COOKIE, USER_KEY
from bystack.infra.users import User, UserError
from bystack.runtime.auth import AuthFailure

router = APIRouter(prefix="/auth", tags=["auth"])


class StatusOut(BaseModel):
    auth_required: bool
    setup_required: bool
    """No account exists yet; the next visitor creates the administrator."""
    setup_token_required: bool
    sso: bool


class MeOut(BaseModel):
    username: str


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class SetupIn(BaseModel):
    # Length bounds only: the rules a person needs to read live in
    # `infra.users.validate` and come back as a sentence, not as a 422 list.
    username: str = Field(max_length=64)
    password: str = Field(max_length=256)
    setup_token: str = Field(default="", max_length=256)


class SsoIn(BaseModel):
    ticket: str = Field(min_length=1, max_length=1024)


@router.get("/status", response_model=StatusOut, summary="Is sign-in needed, and is setup?")
async def status(context: Context) -> StatusOut:
    config = context.settings.auth
    try:
        empty = context.auth.users.count() == 0
    except UserError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return StatusOut(
        auth_required=config.required,
        setup_required=config.required and empty,
        setup_token_required=config.required and empty and bool(config.setup_token),
        sso=bool(config.sso_secret),
    )


@router.get("/me", response_model=MeOut, summary="Who am I signed in as?")
async def me(request: Request) -> MeOut:
    return MeOut(username=request.scope.get(USER_KEY, "anonymous"))


@router.post("/login", response_model=MeOut, summary="Sign in with a password")
async def login(body: LoginIn, request: Request, response: Response, context: Context) -> MeOut:
    user = await _attempt(context.auth.login(body.username, body.password, _ip(request)))
    return _signed_in(user, request, response, context)


@router.post("/setup", response_model=MeOut, status_code=201, summary="Create the first account")
async def setup(body: SetupIn, request: Request, response: Response, context: Context) -> MeOut:
    user = await _attempt(
        context.auth.setup(body.username, body.password, body.setup_token, _ip(request))
    )
    return _signed_in(user, request, response, context)


@router.post("/sso", response_model=MeOut, summary="Sign in with a BYheaven ticket")
async def sso(body: SsoIn, request: Request, response: Response, context: Context) -> MeOut:
    async def attempt() -> User:
        return context.auth.sso(body.ticket, _ip(request))

    user = await _attempt(attempt())
    return _signed_in(user, request, response, context)


@router.post("/logout", status_code=204, summary="Forget this browser's session")
async def logout(response: Response) -> None:
    response.delete_cookie(COOKIE, path="/")


async def _attempt(call: object) -> User:
    try:
        return await call  # type: ignore[no-any-return,misc]
    except AuthFailure as exc:
        headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after else None
        raise HTTPException(status_code=exc.status, detail=exc.detail, headers=headers) from exc


def _signed_in(user: User, request: Request, response: Response, context: Context) -> MeOut:
    secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    response.set_cookie(
        COOKIE,
        context.auth.issue_session(user),
        max_age=context.settings.auth.session_hours * 3600,
        httponly=True,
        samesite="strict",
        secure=secure,
        path="/",
    )
    return MeOut(username=user.username)


def _ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"
