from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Cookie, HTTPException, Response, status
from sqlalchemy import select, update

from sagasmith_service.api.dependencies import CurrentUser, DbSession
from sagasmith_service.config import Settings
from sagasmith_service.models import AuditEvent, QuotaGrant, User, UserSession, now_utc
from sagasmith_service.schemas import (
    AccountDeactivateRequest,
    AuthResult,
    LoginRequest,
    PasswordChangeRequest,
    ProfileUpdateRequest,
    RegisterRequest,
    UserSessionView,
    UserView,
)
from sagasmith_service.security import (
    SESSION_COOKIE,
    create_session,
    hash_password,
    normalize_email,
    token_hash,
    verify_password,
)


def user_view(user: User) -> UserView:
    return UserView(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        status=user.status,
        is_admin=user.is_admin,
        principal_id=user.principal_id,
    )


def router(settings: Settings) -> APIRouter:
    value = APIRouter(prefix="/api/auth", tags=["authentication"])

    def set_cookie(response: Response, token: str) -> None:
        response.set_cookie(
            SESSION_COOKIE,
            token,
            max_age=settings.session_ttl_seconds,
            httponly=True,
            secure=settings.secure_cookies,
            samesite="lax",
            path="/",
        )

    @value.post("/register", response_model=AuthResult, status_code=status.HTTP_201_CREATED)
    def register(payload: RegisterRequest, response: Response, session: DbSession) -> AuthResult:
        email = normalize_email(str(payload.email))
        if session.scalar(select(User.id).where(User.email == email)) is not None:
            raise HTTPException(status.HTTP_409_CONFLICT, "email is already registered")
        user = User(
            email=email,
            password_hash=hash_password(payload.password),
            display_name=payload.display_name.strip(),
            terms_accepted_at=now_utc(),
            terms_version=payload.terms_version,
            privacy_version=payload.privacy_version,
            is_admin=bool(settings.bootstrap_admin_email)
            and email == normalize_email(settings.bootstrap_admin_email),
        )
        session.add(user)
        session.flush()
        quota_start = now_utc()
        session.add(
            QuotaGrant(
                user_id=user.id,
                metric="llm_tokens",
                quantity=Decimal(settings.signup_token_quota),
                period_start=quota_start,
                period_end=quota_start + timedelta(days=30),
                source="signup",
            )
        )
        _, token = create_session(session, user, settings.session_ttl_seconds)
        session.add(
            AuditEvent(
                actor_user_id=user.id,
                action="account.register",
                subject_type="user",
                subject_id=user.id,
            )
        )
        session.commit()
        set_cookie(response, token)
        return AuthResult(user=user_view(user))

    @value.post("/login", response_model=AuthResult)
    def login(payload: LoginRequest, response: Response, session: DbSession) -> AuthResult:
        user = session.scalar(select(User).where(User.email == normalize_email(str(payload.email))))
        if (
            user is None
            or user.status != "active"
            or not verify_password(user.password_hash, payload.password)
        ):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid email or password")
        _, token = create_session(session, user, settings.session_ttl_seconds)
        session.add(
            AuditEvent(
                actor_user_id=user.id,
                action="account.login",
                subject_type="user",
                subject_id=user.id,
            )
        )
        session.commit()
        set_cookie(response, token)
        return AuthResult(user=user_view(user))

    @value.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
    def logout(
        response: Response,
        session: DbSession,
        token: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    ) -> None:
        if token:
            active = session.scalar(
                select(UserSession).where(UserSession.token_hash == token_hash(token))
            )
            if active is not None:
                active.revoked_at = now_utc()
                session.add(
                    AuditEvent(
                        actor_user_id=active.user_id,
                        action="account.logout",
                        subject_type="user_session",
                        subject_id=active.id,
                    )
                )
                session.commit()
        response.delete_cookie(SESSION_COOKIE, path="/")

    @value.get("/me", response_model=AuthResult)
    def me(user: CurrentUser) -> AuthResult:
        return AuthResult(user=user_view(user))

    @value.patch("/me", response_model=AuthResult)
    def update_profile(
        payload: ProfileUpdateRequest, user: CurrentUser, session: DbSession
    ) -> AuthResult:
        display_name = payload.display_name.strip()
        if not display_name:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "display name is required")
        user.display_name = display_name
        session.add(
            AuditEvent(
                actor_user_id=user.id,
                action="account.profile_update",
                subject_type="user",
                subject_id=user.id,
            )
        )
        session.commit()
        return AuthResult(user=user_view(user))

    @value.get("/sessions", response_model=list[UserSessionView])
    def sessions(
        user: CurrentUser,
        session: DbSession,
        token: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    ) -> list[UserSessionView]:
        current_hash = token_hash(token) if token else None
        values = session.scalars(
            select(UserSession)
            .where(
                UserSession.user_id == user.id,
                UserSession.revoked_at.is_(None),
                UserSession.expires_at > now_utc(),
            )
            .order_by(UserSession.last_seen_at.desc())
        )
        return [
            UserSessionView(
                id=value.id,
                created_at=value.created_at,
                last_seen_at=value.last_seen_at,
                expires_at=value.expires_at,
                current=value.token_hash == current_hash,
            )
            for value in values
        ]

    @value.post("/sessions/revoke-others", status_code=status.HTTP_204_NO_CONTENT)
    def revoke_other_sessions(
        user: CurrentUser,
        session: DbSession,
        token: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    ) -> None:
        current_hash = token_hash(token) if token else ""
        session.execute(
            update(UserSession)
            .where(
                UserSession.user_id == user.id,
                UserSession.revoked_at.is_(None),
                UserSession.token_hash != current_hash,
            )
            .values(revoked_at=now_utc())
        )
        session.add(
            AuditEvent(
                actor_user_id=user.id,
                action="account.sessions_revoke_others",
                subject_type="user",
                subject_id=user.id,
            )
        )
        session.commit()

    @value.post("/password", response_model=AuthResult)
    def change_password(
        payload: PasswordChangeRequest,
        response: Response,
        user: CurrentUser,
        session: DbSession,
    ) -> AuthResult:
        if not verify_password(user.password_hash, payload.current_password):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "current password is incorrect")
        if verify_password(user.password_hash, payload.new_password):
            raise HTTPException(status.HTTP_409_CONFLICT, "new password must be different")

        user.password_hash = hash_password(payload.new_password)
        session.execute(
            update(UserSession)
            .where(UserSession.user_id == user.id, UserSession.revoked_at.is_(None))
            .values(revoked_at=now_utc())
        )
        _, token = create_session(session, user, settings.session_ttl_seconds)
        session.add(
            AuditEvent(
                actor_user_id=user.id,
                action="account.password_change",
                subject_type="user",
                subject_id=user.id,
            )
        )
        session.commit()
        set_cookie(response, token)
        return AuthResult(user=user_view(user))

    @value.post("/deactivate", status_code=status.HTTP_204_NO_CONTENT)
    def deactivate(
        payload: AccountDeactivateRequest,
        response: Response,
        user: CurrentUser,
        session: DbSession,
    ) -> None:
        if user.is_admin:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "administrator accounts must transfer duties before deactivation",
            )
        if not verify_password(user.password_hash, payload.current_password):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "current password is incorrect")

        user.status = "deactivated"
        session.execute(
            update(UserSession)
            .where(UserSession.user_id == user.id, UserSession.revoked_at.is_(None))
            .values(revoked_at=now_utc())
        )
        session.add(
            AuditEvent(
                actor_user_id=user.id,
                action="account.deactivate",
                subject_type="user",
                subject_id=user.id,
            )
        )
        session.commit()
        response.delete_cookie(SESSION_COOKIE, path="/")

    return value
