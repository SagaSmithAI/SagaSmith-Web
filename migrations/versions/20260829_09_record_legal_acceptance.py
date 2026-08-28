"""Record the legal text accepted at account creation.

Revision ID: 20260829_09
Revises: 20260828_08
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260829_09"
down_revision: str | None = "20260828_08"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("terms_accepted_at", sa.DateTime(timezone=True)))
    op.add_column("users", sa.Column("terms_version", sa.String(length=24)))
    op.add_column("users", sa.Column("privacy_version", sa.String(length=24)))


def downgrade() -> None:
    op.drop_column("users", "privacy_version")
    op.drop_column("users", "terms_version")
    op.drop_column("users", "terms_accepted_at")
