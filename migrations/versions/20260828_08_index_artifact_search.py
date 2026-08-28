"""index the PostgreSQL Forge artifact search document

Revision ID: 20260828_08
Revises: 20260828_07
Create Date: 2026-08-28
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "20260828_08"
down_revision: Union[str, None] = "20260828_07"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_INDEX_NAME = "ix_artifact_search_document"


def upgrade() -> None:
    op.create_index(
        "ix_artifact_release_latest",
        "artifact_releases",
        ["artifact_id", "status", "published_at", "created_at"],
    )
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        f"""
        CREATE INDEX {_INDEX_NAME}
        ON artifacts USING gin (
            to_tsvector('simple', coalesce(title, '') || ' ' || coalesce(summary, ''))
        )
        """
    )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.drop_index(_INDEX_NAME, table_name="artifacts")
    op.drop_index("ix_artifact_release_latest", table_name="artifact_releases")
