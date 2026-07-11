"""Tests for resync_verified_plugins — startup re-registration of VERIFIED plugins."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import fwbg_agents.orchestrator.plugin_flow as pf
from fwbg_agents.persistence.database import Base
from fwbg_agents.persistence.models import Plugin, PluginState


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    from fwbg_agents.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "plugin_resync_enabled", True)

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/test.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(pf, "SessionLocal", Session)

    yield tmp_path, Session
    await engine.dispose()


def _make_plugin(tmp_path, slug: str) -> Plugin:
    plugin_dir = tmp_path / "plugins" / slug / "v1"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "plugin.py").write_text("# stub\n", encoding="utf-8")
    now = datetime.now(UTC)
    return Plugin(
        slug=slug,
        current_state=PluginState.VERIFIED.value,
        kind="indicator",
        created_at=now,
        updated_at=now,
    )


def _list_client(fqns: list[str]) -> AsyncMock:
    inst = AsyncMock()
    inst.get_plugins = AsyncMock(return_value=[{"fqn": fqn} for fqn in fqns])
    inst.aclose = AsyncMock()
    return inst


async def test_missing_plugin_is_registered(db):
    """VERIFIED plugin absent from fwbg gets re-registered."""
    tmp_path, Session = db

    async with Session() as s:
        s.add(_make_plugin(tmp_path, "my_indicator"))
        await s.commit()

    client = _list_client([])  # fwbg has nothing
    register_calls: list[str] = []

    async def fake_register(plugin, *, agent_run_id=None):
        register_calls.append(plugin.slug)

    with (
        patch("fwbg_agents.orchestrator.plugin_flow.FwbgClient", return_value=client),
        patch.object(pf, "_register_verified_plugin_in_fwbg", side_effect=fake_register),
    ):
        await pf.resync_verified_plugins()

    assert register_calls == ["my_indicator"]
    client.aclose.assert_awaited_once()


async def test_already_registered_plugin_is_skipped(db):
    """VERIFIED plugin already in fwbg is not re-registered."""
    tmp_path, Session = db

    async with Session() as s:
        s.add(_make_plugin(tmp_path, "my_indicator"))
        await s.commit()

    client = _list_client(["agent-authored:my_indicator"])
    register_calls: list[str] = []

    async def fake_register(plugin, *, agent_run_id=None):
        register_calls.append(plugin.slug)

    with (
        patch("fwbg_agents.orchestrator.plugin_flow.FwbgClient", return_value=client),
        patch.object(pf, "_register_verified_plugin_in_fwbg", side_effect=fake_register),
    ):
        await pf.resync_verified_plugins()

    assert register_calls == []


async def test_fwbg_unreachable_does_not_raise(db, monkeypatch):
    """fwbg being offline is logged and swallowed — no exception propagated."""
    import httpx

    client = _list_client([])
    client.get_plugins = AsyncMock(side_effect=httpx.ConnectError("refused"))

    with patch("fwbg_agents.orchestrator.plugin_flow.FwbgClient", return_value=client):
        await pf.resync_verified_plugins()  # must not raise

    client.aclose.assert_awaited_once()


async def test_resync_disabled_skips_everything(db, monkeypatch):
    """plugin_resync_enabled=False means no FwbgClient is created at all."""
    from fwbg_agents.config import settings

    monkeypatch.setattr(settings, "plugin_resync_enabled", False)

    with patch("fwbg_agents.orchestrator.plugin_flow.FwbgClient") as mock_cls:
        await pf.resync_verified_plugins()

    mock_cls.assert_not_called()
