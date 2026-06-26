"""ORM tests for verification_run + extended PluginKind.

Same pattern as test_agent_run.py: tables built via `Base.metadata.create_all`
on a tmp sqlite; the alembic migration itself is exercised by m5_smoke.py and
one explicit upgrade test (no full reversibility loop — sqlite alembic
round-trips have known quirks).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import get_args

import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from fwbg_agents.orchestrator.plugin_contract import PluginKindLit
from fwbg_agents.persistence.database import Base
from fwbg_agents.persistence.models import (
    Plugin,
    PluginKind,
    PluginState,
    VerificationRun,
)


@pytest_asyncio.fixture
async def db(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path}/test.db"
    engine = create_async_engine(db_url, echo=False, future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as session:
        yield session
    await engine.dispose()


def test_plugin_kind_enum_has_all_eight_categories():
    expected = {
        "indicator",
        "model",
        "exit_strategy",
        "risk_management",
        "entry_modifier",
        "preprocessing",
        "feature_selection",
        "data_loading",
    }
    actual = {k.value for k in PluginKind}
    assert actual == expected


def test_plugin_kind_enum_values_match_plugin_contract_literal():
    """PluginContract.kind Literal and PluginKind enum must stay in lockstep."""
    assert {k.value for k in PluginKind} == set(get_args(PluginKindLit))


async def test_insert_verification_run_minimal(db):
    now = datetime.now(UTC)
    plugin = Plugin(
        slug="m5b-vrun-test-1",
        current_state=PluginState.AUTHORED.value,
        kind=PluginKind.INDICATOR.value,
        created_at=now,
        updated_at=now,
    )
    db.add(plugin)
    await db.commit()
    await db.refresh(plugin)

    vr = VerificationRun(
        plugin_id=plugin.id,
        status="running",
        scenarios_run=0,
        scenarios_passed=0,
        started_at=now,
        created_at=now,
    )
    db.add(vr)
    await db.commit()
    await db.refresh(vr)

    assert vr.id is not None
    assert vr.plugin_id == plugin.id
    assert vr.status == "running"
    assert vr.scenarios_run == 0
    assert vr.scenarios_passed == 0
    assert vr.ended_at is None
    assert vr.error_log_path is None


async def test_verification_run_round_trip_with_failed_status(db):
    now = datetime.now(UTC)
    plugin = Plugin(
        slug="m5b-vrun-test-2",
        current_state=PluginState.AUTHORED.value,
        kind=PluginKind.MODEL.value,
        created_at=now,
        updated_at=now,
    )
    db.add(plugin)
    await db.commit()
    await db.refresh(plugin)

    vr = VerificationRun(
        plugin_id=plugin.id,
        status="failed",
        scenarios_run=3,
        scenarios_passed=1,
        error_log_path="data/plugins/m5b-vrun-test-2/v1/error_log.json",
        started_at=now,
        ended_at=now,
        created_at=now,
    )
    db.add(vr)
    await db.commit()

    rows = (
        await db.execute(
            select(VerificationRun).where(VerificationRun.plugin_id == plugin.id)
        )
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].status == "failed"
    assert rows[0].scenarios_passed == 1
    assert rows[0].error_log_path.endswith("error_log.json")


async def test_can_insert_plugin_with_each_new_kind(db):
    """All 8 PluginKind values must be acceptable as plugin.kind strings."""
    now = datetime.now(UTC)
    for k in PluginKind:
        plugin = Plugin(
            slug=f"m5b-kindtest-{k.value}",
            current_state=PluginState.SPECIFIED.value,
            kind=k.value,
            created_at=now,
            updated_at=now,
        )
        db.add(plugin)
    await db.commit()
    rows = (await db.execute(select(Plugin))).scalars().all()
    assert {p.kind for p in rows} == {k.value for k in PluginKind}
