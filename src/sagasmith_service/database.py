from __future__ import annotations

from collections.abc import AsyncGenerator, Generator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool


class Base(DeclarativeBase):
    pass


def make_engine(database_url: str) -> Engine:
    options: dict = {"pool_pre_ping": True}
    if database_url.startswith("sqlite"):
        options["connect_args"] = {"check_same_thread": False}
        if database_url in {"sqlite://", "sqlite:///:memory:"}:
            options["poolclass"] = StaticPool
    engine = create_engine(database_url, **options)
    if database_url.startswith("sqlite"):
        file_backed = engine.url.database not in {None, "", ":memory:"}

        @event.listens_for(engine, "connect")
        def enable_foreign_keys(dbapi_connection, _connection_record) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=5000")
            if file_backed:
                cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

    return engine


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def _async_database_url(database_url: str) -> str:
    url = make_url(database_url)
    if url.drivername in {"sqlite", "sqlite+pysqlite"}:
        if url.database in {None, "", ":memory:"}:
            raise ValueError(
                "selective async paths require file-backed SQLite because anonymous "
                "in-memory databases cannot be shared by sync and async drivers"
            )
        return url.set(drivername="sqlite+aiosqlite").render_as_string(
            hide_password=False
        )
    if url.drivername in {"postgresql", "postgresql+psycopg"}:
        return url.set(drivername="postgresql+psycopg").render_as_string(
            hide_password=False
        )
    raise ValueError(
        "selective async database paths support sqlite or postgresql+psycopg URLs"
    )


def make_async_engine(database_url: str) -> AsyncEngine:
    async_url = _async_database_url(database_url)
    options: dict = {"pool_pre_ping": True}
    if async_url.startswith("sqlite"):
        options["connect_args"] = {"check_same_thread": False}
    engine = create_async_engine(async_url, **options)
    if async_url.startswith("sqlite"):

        @event.listens_for(engine.sync_engine, "connect")
        def enable_foreign_keys(dbapi_connection, _connection_record) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.close()

    return engine


def make_async_session_factory(
    engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def session_dependency(factory: sessionmaker[Session]) -> Generator[Session, None, None]:
    with factory() as session:
        yield session


async def async_session_dependency(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[AsyncSession, None]:
    async with factory() as session:
        try:
            yield session
        finally:
            if session.in_transaction():
                await session.rollback()
