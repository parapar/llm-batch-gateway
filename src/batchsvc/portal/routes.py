"""Student portal: log in with directory credentials, see your budget,
spending, and API key.

Deliberately server-rendered HTML with no JavaScript build step -- it's
four pages of forms and tables, and this way it has no separate deploy
story from the API it reports on.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict, deque
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from batchsvc.config import Settings
from batchsvc.deps import get_db, get_settings
from batchsvc.ldap_auth import LdapAuthFailed, LdapError
from batchsvc.models import User
from batchsvc.portal import service
from batchsvc.portal.service import PortalAccessDenied
from batchsvc.portal.session import COOKIE_NAME, PortalSession, csrf_ok

logger = logging.getLogger("batchsvc.portal")

router = APIRouter(prefix="/portal", tags=["portal"])
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

_GENERIC_LOGIN_ERROR = "Sorry, we couldn't sign you in with those details."


class LoginRateLimiter:
    """Per-client-IP cap on login attempts, so the portal can't be used
    as an oracle to brute-force directory passwords. In-memory and
    per-process, which matches how this service is deployed (one
    process); it resets on restart, which is an accepted limitation
    rather than an oversight."""

    def __init__(self, attempts_per_minute: int):
        self.attempts_per_minute = attempts_per_minute
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, client_ip: str) -> bool:
        if self.attempts_per_minute <= 0:
            return True
        now = time.monotonic()
        hits = self._hits[client_ip]
        while hits and now - hits[0] > 60.0:
            hits.popleft()
        if len(hits) >= self.attempts_per_minute:
            return False
        hits.append(now)
        return True


def _portal_enabled(settings: Settings) -> bool:
    return settings.portal.enabled and bool(settings.portal.session_secret)


def _render(request: Request, template: str, context: dict, status_code: int = 200) -> HTMLResponse:
    return templates.TemplateResponse(request, template, context, status_code=status_code)


def _unavailable(request: Request, message: str) -> HTMLResponse:
    return _render(request, "message.html", {"title": "Portal unavailable", "message": message}, 503)


def _current_session(request: Request) -> PortalSession | None:
    codec = getattr(request.app.state, "portal_sessions", None)
    if codec is None:
        return None
    return codec.loads(request.cookies.get(COOKIE_NAME))


def _current_user(request: Request, db: Session) -> tuple[PortalSession, User] | None:
    session = _current_session(request)
    if session is None:
        return None
    user = db.get(User, session.user_id)
    if user is None or not user.is_active:
        return None
    return session, user


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, settings: Settings = Depends(get_settings)) -> Response:
    if not _portal_enabled(settings):
        return _unavailable(request, "The student portal is not enabled on this server.")
    if _current_session(request) is not None:
        return RedirectResponse(url="/portal/", status_code=303)
    return _render(request, "login.html", {"error": None})


@router.post("/login", response_class=HTMLResponse)
def login_submit(
    request: Request,
    # Defaulted rather than required so a missing or blank field lands in
    # our own validation (and gets the login page back with a readable
    # message) instead of FastAPI's raw JSON 422. ldap_auth rejects empty
    # passwords itself -- see its module docstring on unauthenticated binds.
    username: str = Form(default=""),
    password: str = Form(default=""),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> Response:
    if not _portal_enabled(settings):
        return _unavailable(request, "The student portal is not enabled on this server.")

    authenticator = getattr(request.app.state, "ldap", None)
    if authenticator is None:
        return _unavailable(
            request, "No directory is configured, so logging in isn't possible on this server."
        )

    limiter: LoginRateLimiter = request.app.state.portal_login_limiter
    client_ip = request.client.host if request.client else "unknown"
    if not limiter.allow(client_ip):
        logger.warning("portal login rate limited", extra={"client_ip": client_ip})
        return _render(
            request,
            "login.html",
            {"error": "Too many sign-in attempts. Please wait a minute and try again."},
            429,
        )

    try:
        identity = authenticator.authenticate(username, password)
    except LdapAuthFailed:
        # Same message for wrong password, unknown user, and not-in-group:
        # the portal shouldn't confirm who exists in the directory.
        logger.info("portal login failed", extra={"username": username, "client_ip": client_ip})
        return _render(request, "login.html", {"error": _GENERIC_LOGIN_ERROR}, 401)
    except LdapError:
        logger.exception("portal login could not reach the directory")
        return _render(
            request,
            "login.html",
            {"error": "The directory is unreachable right now. Please try again shortly."},
            503,
        )

    try:
        user = service.resolve_user(db, identity, settings.portal)
    except PortalAccessDenied as e:
        logger.info("portal access denied", extra={"username": identity.username, "reason": str(e)})
        return _render(
            request,
            "login.html",
            {"error": "Your directory account isn't set up for this service. Ask your instructor."},
            403,
        )

    codec = request.app.state.portal_sessions
    session = codec.new_session(user_id=user.id, username=user.username)
    response = RedirectResponse(url="/portal/", status_code=303)
    response.set_cookie(
        COOKIE_NAME,
        codec.dumps(session),
        max_age=settings.portal.session_lifetime_minutes * 60,
        httponly=True,
        samesite="lax",
        secure=settings.portal.cookie_secure,
        path="/portal",
    )
    logger.info("portal login", extra={"username": user.username})
    return response


@router.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)
) -> Response:
    if not _portal_enabled(settings):
        return _unavailable(request, "The student portal is not enabled on this server.")
    current = _current_user(request, db)
    if current is None:
        return RedirectResponse(url="/portal/login", status_code=303)
    session, user = current
    data = service.load_dashboard(db, user)
    return _render(
        request, "dashboard.html", {"d": data, "csrf_token": session.csrf_token, "new_key": None}
    )


@router.post("/api-key", response_class=HTMLResponse)
def rotate_api_key(
    request: Request,
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> Response:
    if not _portal_enabled(settings):
        return _unavailable(request, "The student portal is not enabled on this server.")
    current = _current_user(request, db)
    if current is None:
        return RedirectResponse(url="/portal/login", status_code=303)
    session, user = current
    if not csrf_ok(session, csrf_token):
        return _render(
            request,
            "message.html",
            {"title": "Bad request", "message": "Invalid form token. Please reload and try again."},
            400,
        )

    raw_key = service.regenerate_api_key(db, user)
    data = service.load_dashboard(db, user)
    # Rendered straight into this response rather than redirecting: the
    # raw key exists only here, and stashing it in a cookie/session to
    # survive a redirect would be storing exactly what we don't store.
    return _render(
        request, "dashboard.html", {"d": data, "csrf_token": session.csrf_token, "new_key": raw_key}
    )


@router.post("/logout")
def logout(request: Request, csrf_token: str = Form(...), db: Session = Depends(get_db)) -> Response:
    current = _current_user(request, db)
    response = RedirectResponse(url="/portal/login", status_code=303)
    if current is not None and csrf_ok(current[0], csrf_token):
        response.delete_cookie(COOKIE_NAME, path="/portal")
    return response
