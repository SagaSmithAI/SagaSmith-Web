from __future__ import annotations

from typing import Annotated

from fastapi import Cookie, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from sagasmith_service.models import User
from sagasmith_service.security import SESSION_COOKIE, authenticate_session


def get_db(request: Request):
    factory = request.app.state.session_factory
    with factory() as session:
        yield session


DbSession = Annotated[Session, Depends(get_db)]


def current_user(
    session: DbSession,
    token: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
) -> User:
    user = authenticate_session(session, token)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "authentication required")
    return user


CurrentUser = Annotated[User, Depends(current_user)]
