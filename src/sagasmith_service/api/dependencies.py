from __future__ import annotations

from typing import Annotated

from fastapi import Cookie, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from sagasmith_service.models import User, now_utc
from sagasmith_service.security import SESSION_COOKIE, authenticate_session


def get_db(request: Request):
    factory = request.app.state.session_factory
    with factory() as session:
        yield session


DbSession = Annotated[Session, Depends(get_db)]


async def get_async_db(request: Request):
    factory = request.app.state.async_session_factory
    async with factory() as session:
        try:
            yield session
        finally:
            if session.in_transaction():
                await session.rollback()


AsyncDbSession = Annotated[AsyncSession, Depends(get_async_db)]


async def async_current_user(
    session: AsyncDbSession,
    token: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
) -> User:
    authenticated = await session.run_sync(
        lambda sync_session: authenticate_session(sync_session, token)
    )
    if authenticated is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "authentication required")
    user, active_session = authenticated
    active_session.last_seen_at = now_utc()
    await session.commit()
    return user


AsyncCurrentUser = Annotated[User, Depends(async_current_user)]


def current_user(
    session: DbSession,
    token: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
) -> User:
    authenticated = authenticate_session(session, token)
    if authenticated is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "authentication required")
    user, active_session = authenticated
    active_session.last_seen_at = now_utc()
    # FastAPI resolves this dependency before entering the endpoint. Committing the heartbeat
    # here gives it an explicit transaction boundary before route code can stage other writes.
    session.commit()
    return user


CurrentUser = Annotated[User, Depends(current_user)]
