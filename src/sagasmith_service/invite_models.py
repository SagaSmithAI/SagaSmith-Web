from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from sagasmith_service.database import Base
from sagasmith_service.models import TimestampMixin, new_id


class RegistrationInvite(TimestampMixin, Base):
    """One-time or bounded-use token for beta account registration."""

    __tablename__ = "registration_invites"
    __table_args__ = (
        CheckConstraint("max_uses > 0", name="ck_registration_invite_max_uses_positive"),
        CheckConstraint("used_count >= 0", name="ck_registration_invite_used_count_nonnegative"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    email: Mapped[str | None] = mapped_column(String(320), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    max_uses: Mapped[int] = mapped_column(default=1)
    used_count: Mapped[int] = mapped_column(default=0)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
