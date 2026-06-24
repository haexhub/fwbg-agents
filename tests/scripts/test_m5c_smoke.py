"""Self-test for scripts/m5c_smoke.py.

Runs the smoke end-to-end against a tmp_path data_dir and a fresh sqlite
DB so the dev DB is untouched. Mirrors the pattern from
`tests/api/test_plugin_flow_api.py` for the in-memory DB + ASGI transport,
plus the FunctionModel stub from the smoke itself.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from fwbg_agents.persistence.database import Base
from fwbg_agents.persistence.models import Plugin, PluginState, Strategy


@pytest_asyncio.fixture
async def isolated_smoke_env(tmp_path, monkeypatch):
    """Redirect data_dir + SessionLocal at tmp_path so the smoke runs hermetically.

    Patches in three places:
      - settings.data_dir       — strategies/, plugins/ artifacts
      - settings.fwbg_repo_root — keep load_catalog from picking up real fwbg
      - SessionLocal everywhere a smoke / background task imports it
    Also clears the fwbg-discovery cache so the in-memory plugin row is the
    only catalog source.
    """
    from fwbg_agents import main as fwbg_main
    from fwbg_agents.api import plugins as plugins_api
    from fwbg_agents.config import settings
    from fwbg_agents.orchestrator.plugin_catalog import _load_fwbg_cached
    from fwbg_agents.persistence import database as db_module

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(settings, "data_dir", data_dir)
    fwbg_root = tmp_path / "no-fwbg"
    fwbg_root.mkdir()
    monkeypatch.setattr(settings, "fwbg_repo_root", fwbg_root)
    _load_fwbg_cached.cache_clear()

    db_url = f"sqlite+aiosqlite:///{tmp_path}/smoke.db"
    engine = create_async_engine(db_url, future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    TestSession = async_sessionmaker(engine, expire_on_commit=False)

    # Patch SessionLocal in every module that imported the symbol directly.
    import scripts.m5c_smoke as m5c_smoke

    monkeypatch.setattr(db_module, "SessionLocal", TestSession)
    monkeypatch.setattr(plugins_api, "SessionLocal", TestSession)
    monkeypatch.setattr(m5c_smoke, "SessionLocal", TestSession)

    # Override get_session for the synchronous endpoint precondition checks.
    async def _override_get_session():
        async with TestSession() as session:
            yield session

    from fwbg_agents.persistence.database import get_session

    fwbg_main.app.dependency_overrides[get_session] = _override_get_session

    yield TestSession

    fwbg_main.app.dependency_overrides.clear()
    _load_fwbg_cached.cache_clear()
    await engine.dispose()


async def test_m5c_smoke_self_test(isolated_smoke_env):
    """Full chain: parent → author → evaluate → reiterate → child PROPOSED."""
    import scripts.m5c_smoke as m5c_smoke

    rc = await m5c_smoke.main()
    assert rc == 0, "m5c_smoke.main() returned non-zero"

    TestSession = isolated_smoke_env
    async with TestSession() as session:
        parent = (
            await session.execute(
                select(Strategy).where(Strategy.slug == m5c_smoke.SMOKE_STRATEGY_SLUG)
            )
        ).scalar_one()
        children = (
            await session.execute(
                select(Strategy).where(Strategy.parent_strategy_id == parent.id)
            )
        ).scalars().all()
        plugin = (
            await session.execute(
                select(Plugin).where(Plugin.slug == m5c_smoke.SMOKE_PLUGIN_SLUG)
            )
        ).scalar_one()

    assert plugin.current_state == PluginState.VERIFIED.value
    assert len(children) == 1
    child = children[0]
    assert child.parent_strategy_id == parent.id

    # The child's strategy.json must carry the plugin slug under indicators[].
    from fwbg_agents.config import settings

    child_strategy_path = (
        settings.data_dir
        / "strategies"
        / child.slug
        / "iteration_001"
        / "strategy.json"
    )
    payload = json.loads(child_strategy_path.read_text())
    assert payload["indicators"] == [m5c_smoke.SMOKE_PLUGIN_SLUG]


async def test_m5c_smoke_idempotent_against_existing_plugin(isolated_smoke_env):
    """Pre-seeding a plugin row with the smoke's slug must abort cleanly.

    Protects against the M5b regression where stale dev-DB rows broke re-runs
    by aborting the smoke unhelpfully. The smoke returns 1 BEFORE doing any
    HTTP work, so no child Strategy is created.
    """
    import scripts.m5c_smoke as m5c_smoke

    TestSession = isolated_smoke_env
    now = datetime.now(UTC)
    async with TestSession() as session:
        session.add(
            Plugin(
                slug=m5c_smoke.SMOKE_PLUGIN_SLUG,
                current_state=PluginState.VERIFIED.value,
                kind="indicator",
                spec_path=f"data/plugins/{m5c_smoke.SMOKE_PLUGIN_SLUG}/v1/spec.md",
                contract_path=f"data/plugins/{m5c_smoke.SMOKE_PLUGIN_SLUG}/v1/contract.yaml",
                created_at=now,
                updated_at=now,
            )
        )
        await session.commit()

    rc = await m5c_smoke.main()
    assert rc == 1, "smoke must refuse to run when its plugin slug is taken"

    async with TestSession() as session:
        children = (
            await session.execute(
                select(Strategy).where(Strategy.parent_strategy_id.is_not(None))
            )
        ).scalars().all()
    assert children == [], "smoke must not create a child when aborting"
