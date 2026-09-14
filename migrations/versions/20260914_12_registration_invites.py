"""add bounded beta registration invites

Revision ID: 20260914_12
Revises: 20260914_11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260914_12"
down_revision: str | None = "20260914_11"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "registration_invites",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("max_uses", sa.Integer(), nullable=False),
        sa.Column("used_count", sa.Integer(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("max_uses > 0", name="ck_registration_invite_max_uses_positive"),
        sa.CheckConstraint("used_count >= 0", name="ck_registration_invite_used_count_nonnegative"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_registration_invites_email", "registration_invites", ["email"])
    op.create_index("ix_registration_invites_expires_at", "registration_invites", ["expires_at"])
    op.create_index("ix_registration_invites_token_hash", "registration_invites", ["token_hash"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_registration_invites_token_hash", table_name="registration_invites")
    op.drop_index("ix_registration_invites_expires_at", table_name="registration_invites")
    op.drop_index("ix_registration_invites_email", table_name="registration_invites")
    op.drop_table("registration_invites")
