"""The app engine must run SQLite in WAL mode with a busy timeout —
otherwise concurrent writers (research flows, runner polling, API) die
with "database is locked"."""

from __future__ import annotations

from sqlalchemy import text

from fwbg_agents.persistence.database import SessionLocal, engine


async def test_engine_applies_wal_and_busy_timeout():
    assert engine.dialect.name == "sqlite"
    async with SessionLocal() as session:
        journal_mode = (await session.execute(text("PRAGMA journal_mode"))).scalar_one()
        busy_timeout = (await session.execute(text("PRAGMA busy_timeout"))).scalar_one()
    assert journal_mode.lower() == "wal"
    assert busy_timeout >= 30000
