"""Signed-cookie sessions for the student portal.

No server-side session store: the cookie carries {user_id, username,
csrf} signed with itsdangerous, and itsdangerous's timestamp check
enforces the lifetime. That keeps the portal stateless (no session table
to clean up, no invalidation to get wrong on restart) at the cost of not
being able to revoke an individual session before it expires -- an
acceptable trade for a class portal whose sessions last hours, and one
that disabling the account still covers, since every request re-checks
the user is active.
"""

from __future__ import annotations

import hmac
import secrets
from dataclasses import dataclass

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

COOKIE_NAME = "batchsvc_portal"
_SALT = "batchsvc.portal.session"


@dataclass(frozen=True)
class PortalSession:
    user_id: str
    username: str
    csrf_token: str


class SessionCodec:
    def __init__(self, secret: str, *, lifetime_minutes: int):
        if not secret:
            raise ValueError("portal session_secret must be set")
        self._serializer = URLSafeTimedSerializer(secret, salt=_SALT)
        self._max_age = lifetime_minutes * 60

    def new_session(self, *, user_id: str, username: str) -> PortalSession:
        return PortalSession(user_id=user_id, username=username, csrf_token=secrets.token_urlsafe(32))

    def dumps(self, session: PortalSession) -> str:
        return self._serializer.dumps(
            {"user_id": session.user_id, "username": session.username, "csrf": session.csrf_token}
        )

    def loads(self, raw: str | None) -> PortalSession | None:
        if not raw:
            return None
        try:
            data = self._serializer.loads(raw, max_age=self._max_age)
        except (BadSignature, SignatureExpired):
            return None
        if not isinstance(data, dict):
            return None
        user_id, username, csrf = data.get("user_id"), data.get("username"), data.get("csrf")
        if not (isinstance(user_id, str) and isinstance(username, str) and isinstance(csrf, str)):
            return None
        return PortalSession(user_id=user_id, username=username, csrf_token=csrf)


def csrf_ok(session: PortalSession, submitted: str | None) -> bool:
    if not submitted:
        return False
    return hmac.compare_digest(session.csrf_token, submitted)
