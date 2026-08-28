from __future__ import annotations

import asyncio

from sqlalchemy import func, select

from sagasmith_service.database import (
    Base,
    make_async_engine,
    make_async_session_factory,
    make_engine,
    make_session_factory,
)
from sagasmith_service.models import (
    CampaignMembershipProjection,
    CampaignPanelProjection,
    CampaignProjection,
    OutboxEvent,
    User,
)
from sagasmith_service.projection_cache import (
    load_panel_projection,
    store_panel_projection,
)
from sagasmith_service.realtime import (
    OutboxDispatcher,
    RealtimeHub,
    install_transactional_outbox,
)


def _database(tmp_path):
    url = f"sqlite:///{(tmp_path / 'realtime.db').as_posix()}"
    engine = make_engine(url)
    Base.metadata.create_all(engine)
    return url, engine, make_session_factory(engine)


def _user() -> User:
    return User(
        id="user-1",
        email="realtime@example.com",
        password_hash="not-used",
        display_name="Realtime",
    )


def _campaign() -> CampaignProjection:
    return CampaignProjection(
        id="campaign-1",
        name="Realtime campaign",
        owner_user_id="user-1",
        mcp_revision=1,
    )


def test_projection_and_access_outbox_share_the_authoritative_transaction(tmp_path) -> None:
    _url, engine, factory = _database(tmp_path)
    install_transactional_outbox()

    with factory() as session:
        session.add(_user())
        session.flush()
        session.add(_campaign())
        session.flush()
        session.add(
            CampaignMembershipProjection(
                id="membership-1",
                campaign_id="campaign-1",
                user_id="user-1",
                role="player",
            )
        )
        session.flush()
        assert session.scalar(select(func.count(OutboxEvent.id))) == 2
        session.rollback()

    with factory() as session:
        assert session.scalar(select(func.count(OutboxEvent.id))) == 0
        session.add(_user())
        session.flush()
        session.add(_campaign())
        session.flush()
        session.add(
            CampaignMembershipProjection(
                id="membership-1",
                campaign_id="campaign-1",
                user_id="user-1",
                role="player",
            )
        )
        session.commit()

    with factory() as session:
        membership = session.get(CampaignMembershipProjection, "membership-1")
        assert membership is not None
        assert membership.authorization_epoch == 1
        membership.role = "dm"
        session.commit()
        assert membership.authorization_epoch == 2
        access_event = session.scalar(
            select(OutboxEvent)
            .where(OutboxEvent.event_type == "access.changed")
            .order_by(OutboxEvent.created_at.desc())
        )
        assert access_event is not None
        assert access_event.payload["authorization_epoch"] == 2
        assert access_event.payload["topics"] == [
            "campaign:campaign-1",
            "principal:user-1",
        ]

    engine.dispose()


def test_outbox_dispatcher_fans_out_only_realtime_rows(tmp_path) -> None:
    _url, engine, factory = _database(tmp_path)
    with factory() as session:
        session.add_all(
            [
                OutboxEvent(
                    id="realtime-event",
                    event_type="state.changed",
                    aggregate_type="campaign",
                    aggregate_id="campaign-1",
                    payload={"topics": ["campaign:campaign-1"], "revision": 2},
                    idempotency_key="realtime:state-change",
                ),
                OutboxEvent(
                    id="other-event",
                    event_type="future.worker.command",
                    aggregate_type="worker",
                    aggregate_id="worker-1",
                    payload={},
                    idempotency_key="worker:command",
                ),
            ]
        )
        session.commit()

    async def exercise() -> None:
        hub = RealtimeHub()
        await hub.start()
        dispatcher = OutboxDispatcher(factory, hub, poll_seconds=0.01)
        async with hub.subscribe({"campaign:campaign-1"}) as queue:
            dispatcher.start()
            notice = await asyncio.wait_for(queue.get(), timeout=2)
            assert notice.event_id == "realtime-event"
            assert notice.payload["revision"] == 2
        await dispatcher.close()
        await hub.close()

    asyncio.run(exercise())
    with factory() as session:
        assert session.get(OutboxEvent, "realtime-event").status == "completed"
        assert session.get(OutboxEvent, "other-event").status == "pending"
    engine.dispose()


def test_panel_projection_requires_matching_revision_and_authorization_epoch(tmp_path) -> None:
    url, engine, factory = _database(tmp_path)
    install_transactional_outbox()
    with factory() as session:
        session.add(_user())
        session.flush()
        session.add(_campaign())
        session.commit()

    async def exercise() -> None:
        async_engine = make_async_engine(url)
        async_factory = make_async_session_factory(async_engine)
        async with async_factory() as session:
            await store_panel_projection(
                session,
                campaign_id="campaign-1",
                audience_key="principal:user-1",
                source_revision=7,
                authorization_epoch=3,
                payload={"revision": 7, "visible": "party"},
            )
            await session.commit()
        async with async_factory() as session:
            assert await load_panel_projection(
                session,
                campaign_id="campaign-1",
                audience_key="principal:user-1",
                source_revision=7,
                authorization_epoch=3,
            ) == {"revision": 7, "visible": "party"}
            assert (
                await load_panel_projection(
                    session,
                    campaign_id="campaign-1",
                    audience_key="principal:user-1",
                    source_revision=8,
                    authorization_epoch=3,
                )
                is None
            )
            assert (
                await load_panel_projection(
                    session,
                    campaign_id="campaign-1",
                    audience_key="principal:user-1",
                    source_revision=7,
                    authorization_epoch=4,
                )
                is None
            )
            assert await session.scalar(select(func.count(CampaignPanelProjection.id))) == 1
        await async_engine.dispose()

    asyncio.run(exercise())
    engine.dispose()
