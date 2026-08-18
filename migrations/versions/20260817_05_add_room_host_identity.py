"""add explicit hosted room identity

Revision ID: 20260817_05
Revises: 20260816_04
Create Date: 2026-08-17
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260817_05"
down_revision: Union[str, None] = "20260816_04"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("campaign_rooms") as batch_op:
        batch_op.add_column(
            sa.Column("host_identity_assignment_id", sa.String(length=36), nullable=True)
        )
        batch_op.create_foreign_key(
            "fk_campaign_rooms_host_identity_assignment",
            "identity_campaign_assignments",
            ["host_identity_assignment_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_index(
            "ix_campaign_rooms_host_identity_assignment_id",
            ["host_identity_assignment_id"],
        )


def downgrade() -> None:
    with op.batch_alter_table("campaign_rooms") as batch_op:
        batch_op.drop_index("ix_campaign_rooms_host_identity_assignment_id")
        batch_op.drop_constraint(
            "fk_campaign_rooms_host_identity_assignment",
            type_="foreignkey",
        )
        batch_op.drop_column("host_identity_assignment_id")
