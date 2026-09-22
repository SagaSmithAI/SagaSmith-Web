"""Durable room operation fences survive model and worker failure."""
import sqlalchemy as sa
from alembic import op

revision = "20260915_14"
down_revision = "20260914_13"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "room_operations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("job_id", sa.String(36), sa.ForeignKey("room_turn_jobs.id"), nullable=False),
        sa.Column("tool", sa.String(160), nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_room_operations_job_id", "room_operations", ["job_id"])


def downgrade():
    op.drop_index("ix_room_operations_job_id", "room_operations")
    op.drop_table("room_operations")
