from __future__ import annotations

from contextvars import ContextVar, Token

from sqlalchemy import event

from sagasmith_service.models import AuditEvent

_request_id: ContextVar[str | None] = ContextVar("sagasmith_request_id", default=None)


def bind_request_id(value: str) -> Token[str | None]:
    return _request_id.set(value)


def reset_request_id(token: Token[str | None]) -> None:
    _request_id.reset(token)


@event.listens_for(AuditEvent, "before_insert")
def attach_request_id(_mapper, _connection, target: AuditEvent) -> None:
    if target.request_id is None:
        target.request_id = _request_id.get()
