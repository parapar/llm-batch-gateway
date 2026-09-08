"""FastAPI dependencies: DB session and authentication."""

from __future__ import annotations

from collections.abc import Iterator

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from batchsvc.config import Settings
from batchsvc.db import Database
from batchsvc.errors import AuthenticationError, PermissionDeniedError
from batchsvc.models import ApiKey, User
from batchsvc.security import constant_time_eq, hash_api_key

_bearer = HTTPBearer(auto_error=False)


def get_database(request: Request) -> Database:
    return request.app.state.db


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_db(db: Database = Depends(get_database)) -> Iterator[Session]:
    session = db.session()
    try:
        yield session
    finally:
        session.close()


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: Session = Depends(get_db),
) -> User:
    if credentials is None or not credentials.credentials:
        raise AuthenticationError("Missing API key. Pass it as 'Authorization: Bearer sk-...'.")
    raw_key = credentials.credentials
    key_hash = hash_api_key(raw_key)
    api_key = db.query(ApiKey).filter(ApiKey.key_hash == key_hash).one_or_none()
    if api_key is None or api_key.is_revoked:
        raise AuthenticationError()
    user = db.get(User, api_key.user_id)
    if user is None or not user.is_active:
        raise AuthenticationError("This account is disabled.")
    return user


def require_admin(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    settings: Settings = Depends(get_settings),
) -> None:
    if credentials is None or not credentials.credentials:
        raise AuthenticationError("Missing admin token.")
    if not settings.admin_token or not constant_time_eq(credentials.credentials, settings.admin_token):
        raise PermissionDeniedError("Invalid admin token.")
