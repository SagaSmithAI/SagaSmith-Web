from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from sagasmith_service.models import User, UserSession, now_utc

SESSION_COOKIE = "sagasmith_session"
_PASSWORDS = PasswordHasher()


def normalize_email(value: str) -> str:
    return value.strip().casefold()


def hash_password(password: str) -> str:
    if len(password) < 12:
        raise ValueError("password must contain at least 12 characters")
    return _PASSWORDS.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _PASSWORDS.verify(password_hash, password)
    except (InvalidHashError, VerificationError):
        return False


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_session(session: Session, user: User, ttl_seconds: int) -> tuple[UserSession, str]:
    token = secrets.token_urlsafe(48)
    value = UserSession(
        user_id=user.id,
        token_hash=token_hash(token),
        expires_at=now_utc() + timedelta(seconds=ttl_seconds),
    )
    session.add(value)
    session.flush()
    return value, token


def authenticate_session(
    session: Session, token: str | None
) -> tuple[User, UserSession] | None:
    if not token:
        return None
    now = now_utc()
    value = session.scalar(
        select(UserSession).where(
            UserSession.token_hash == token_hash(token),
            UserSession.revoked_at.is_(None),
            UserSession.expires_at > now,
        )
    )
    if value is None:
        return None
    user = session.get(User, value.user_id)
    if user is None or user.status != "active":
        return None
    return user, value
