"""Async SQLAlchemy engine + session factory."""

from collections.abc import AsyncIterator

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from fwbg_agents.config import settings


class Base(DeclarativeBase):
    pass


engine = create_async_engine(settings.db_url, echo=False, future=True)

if engine.dialect.name == "sqlite":

    @event.listens_for(engine.sync_engine, "connect")
    def _sqlite_concurrency_pragmas(dbapi_connection, _record) -> None:
        """Make SQLite survive concurrent writers.

        Research flows, the runner's progress polling, and API requests all
        write from separate connections; with the default rollback journal
        and no busy timeout a second writer fails instantly with "database is
        locked" — seen live killing a research flow AND the error handler
        that tried to record the failure. WAL allows readers alongside one
        writer; the busy timeout makes a blocked writer wait instead of
        raising.
        """
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()


SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session
