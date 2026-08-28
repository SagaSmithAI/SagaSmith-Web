from __future__ import annotations

from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from sagasmith_service.models import CampaignPanelProjection

PANEL_PROJECTION_SCHEMA_VERSION = 1


async def load_panel_projection(
    session: AsyncSession,
    *,
    campaign_id: str,
    audience_key: str,
    source_revision: int,
    authorization_epoch: int,
) -> dict[str, Any] | None:
    item = await session.scalar(
        select(CampaignPanelProjection).where(
            CampaignPanelProjection.campaign_id == campaign_id,
            CampaignPanelProjection.audience_key == audience_key,
            CampaignPanelProjection.source_revision == source_revision,
            CampaignPanelProjection.authorization_epoch == authorization_epoch,
            CampaignPanelProjection.projection_schema_version
            == PANEL_PROJECTION_SCHEMA_VERSION,
        )
    )
    return dict(item.payload) if item is not None else None


async def store_panel_projection(
    session: AsyncSession,
    *,
    campaign_id: str,
    audience_key: str,
    source_revision: int,
    authorization_epoch: int,
    payload: dict[str, Any],
) -> None:
    item = await session.scalar(
        select(CampaignPanelProjection).where(
            CampaignPanelProjection.campaign_id == campaign_id,
            CampaignPanelProjection.audience_key == audience_key,
        )
    )
    if item is None:
        item = CampaignPanelProjection(
            campaign_id=campaign_id,
            audience_key=audience_key,
        )
        session.add(item)
    item.source_revision = source_revision
    item.authorization_epoch = authorization_epoch
    item.projection_schema_version = PANEL_PROJECTION_SCHEMA_VERSION
    item.payload = payload


async def invalidate_panel_projections(
    session: AsyncSession,
    *,
    campaign_id: str,
    audience_key: str | None = None,
) -> int:
    statement = delete(CampaignPanelProjection).where(
        CampaignPanelProjection.campaign_id == campaign_id
    )
    if audience_key is not None:
        statement = statement.where(CampaignPanelProjection.audience_key == audience_key)
    result = await session.execute(statement)
    return int(result.rowcount or 0)
