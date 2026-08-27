import asyncio
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from sagasmith_service.config import Settings
from sagasmith_service.database import (
    async_session_dependency,
    make_async_engine,
    make_async_session_factory,
    make_engine,
)
from sagasmith_service.main import create_app
from sagasmith_service.models import User


def test_async_database_url_uses_the_supported_driver(tmp_path) -> None:
    sqlite_url = f"sqlite:///{(tmp_path / 'async-driver.db').as_posix()}"
    sqlite_engine = make_async_engine(sqlite_url)
    postgres_engine = make_async_engine(
        "postgresql+psycopg://user:password@localhost:5432/sagasmith"
    )

    assert sqlite_engine.url.drivername == "sqlite+aiosqlite"
    assert postgres_engine.url.drivername == "postgresql+psycopg"

    asyncio.run(sqlite_engine.dispose())
    asyncio.run(postgres_engine.dispose())


def test_anonymous_in_memory_sqlite_is_rejected_for_dual_engine_use() -> None:
    with pytest.raises(ValueError, match="file-backed SQLite"):
        make_async_engine("sqlite://")


def test_file_sqlite_enables_wal_and_busy_timeout_for_both_engines(tmp_path) -> None:
    database_url = f"sqlite:///{(tmp_path / 'sqlite-pragmas.db').as_posix()}"
    sync_engine = make_engine(database_url)
    async_engine = make_async_engine(database_url)

    async def inspect_async() -> None:
        # The async hot path can be the first connection in a production
        # process, before readiness or a synchronous CRUD route opens the
        # synchronous engine.
        async with async_engine.connect() as connection:
            assert await connection.scalar(text("PRAGMA journal_mode")) == "wal"
            assert await connection.scalar(text("PRAGMA busy_timeout")) == 5000
        await async_engine.dispose()

    asyncio.run(inspect_async())

    with sync_engine.connect() as connection:
        assert connection.scalar(text("PRAGMA journal_mode")) == "wal"
        assert connection.scalar(text("PRAGMA busy_timeout")) == 5000


def test_async_session_dependency_rolls_back_uncommitted_work(tmp_path) -> None:
    database_url = f"sqlite:///{(tmp_path / 'rollback.db').as_posix()}"
    sync_engine = make_engine(database_url)
    User.metadata.create_all(sync_engine)
    async_engine = make_async_engine(database_url)
    factory = make_async_session_factory(async_engine)

    async def exercise() -> None:
        dependency = async_session_dependency(factory)
        session = await anext(dependency)
        session.add(
            User(
                email="rolled-back@example.com",
                password_hash="not-a-real-password-hash",
                display_name="Rolled back",
            )
        )
        await session.flush()
        await dependency.aclose()
        await async_engine.dispose()

    asyncio.run(exercise())

    with sync_engine.connect() as connection:
        assert connection.scalar(select(User.id)) is None


def test_app_lifespan_disposes_the_async_engine(tmp_path, monkeypatch) -> None:
    database_url = f"sqlite:///{(tmp_path / 'lifecycle.db').as_posix()}"
    sync_engine = make_engine(database_url)
    async_engine = make_async_engine(database_url)
    dispose = AsyncMock()
    monkeypatch.setattr(type(async_engine), "dispose", dispose)
    app = create_app(
        Settings(env="test", database_url=database_url),
        sync_engine,
        async_engine=async_engine,
    )

    with TestClient(app) as client:
        assert client.get("/api/health").status_code == 200

    dispose.assert_awaited_once()
