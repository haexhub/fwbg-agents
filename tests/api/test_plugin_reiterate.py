"""Tests for the M5c reiterate-with-plugin endpoint.

POST /strategies/{id}/reiterate-with-plugin   → 202 + AgentRun envelope

Behaviour-only: in-process FastAPI via ASGI transport; background task is
monkeypatched so the endpoint's validation + AgentRun bookkeeping is the
focus. End-to-end execution is covered by scripts/m5c_smoke.py (Task 4).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from fwbg_agents.main import app
from fwbg_agents.orchestrator.plugin_catalog import _load_fwbg_cached
from fwbg_agents.persistence.database import Base, get_session
from fwbg_agents.persistence.models import (
    AgentRun,
    AgentRunStatus,
    Plugin,
    PluginState,
    Strategy,
    StrategyState,
)


PARENT_STRATEGY_JSON = {
    "name": "orb__forex__001",
    "description": "ORB rule-based on FOREX majors",
    "hypothesis": "Opening range breakouts on EURUSD M15.",
    "expected_outcome": "sharpe > 1.0",
    "datasource": "forexsb",
    "pipeline": "orb_simple_v1",
    "model": "signal_orb_v1",
    "filters": "orb_scalping_v1",
    "validation": "walk_forward_intraday_v1",
    "resources": "standard_v1",
    "timeframe": "MINUTE_15",
    "exit_strategies": [
        {
            "name": "orb_based",
            "params": {"sl_mult": 1.0, "tp_mult": 5.0, "atr_period": 14},
            "ct": [0.5],
        },
    ],
    "tags": ["orb", "intraday", "forex_majors"],
    "optimization": {"grid_params": {"sl_mult": [0.9, 1.0, 1.1]}},
}

PARENT_HYPOTHESIS = {
    "title": "ORB on FOREX majors",
    "asset_class": "FOREX",
    "strategy_family": "ORB",
    "hypothesis": "OR breakouts on EURUSD M15.",
    "expected_edge_explanation": "Liquidity formation in early London.",
    "key_indicators": ["opening_range", "atr"],
    "tags": ["orb", "intraday", "forex_majors"],
    "sources": [{"url": "https://x", "title": "x", "why_relevant": "x"}],
    "differentiates_from": [],
}

PLUGIN_SLUG = "adx-trend-strength"
DEFAULT_CAPABILITY = "detect strong trends"


@pytest_asyncio.fixture
async def client_with_db(tmp_path, monkeypatch):
    from fwbg_agents.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    empty_fwbg_root = tmp_path / "empty_fwbg"
    empty_fwbg_root.mkdir()
    monkeypatch.setattr(settings, "fwbg_repo_root", empty_fwbg_root)
    _load_fwbg_cached.cache_clear()

    db_url = f"sqlite+aiosqlite:///{tmp_path}/reiter.db"
    engine = create_async_engine(db_url, future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    session = Session()

    async def _override_get_session():
        yield session

    app.dependency_overrides[get_session] = _override_get_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        yield client, session, settings, Session

    app.dependency_overrides.clear()
    await session.close()
    await engine.dispose()
    _load_fwbg_cached.cache_clear()


async def _seed_parent_in_state(
    session,
    settings,
    *,
    slug: str,
    state: str,
    with_sidecar: bool,
    capability: str = DEFAULT_CAPABILITY,
) -> Strategy:
    """Seed Strategy + (optionally) iteration_001 files including sidecar."""
    now = datetime.now(UTC)
    s = Strategy(
        slug=slug,
        current_state=state,
        iteration_count=1,
        asset_class="FOREX",
        strategy_family="ORB",
        created_at=now,
        updated_at=now,
    )
    session.add(s)
    await session.commit()
    await session.refresh(s)

    it_dir = settings.data_dir / "strategies" / slug / "iteration_001"
    it_dir.mkdir(parents=True, exist_ok=True)
    (it_dir / "strategy.json").write_text(json.dumps(PARENT_STRATEGY_JSON, indent=2))
    (it_dir / "hypothesis.json").write_text(json.dumps(PARENT_HYPOTHESIS, indent=2))
    if with_sidecar:
        (it_dir / "add_indicator_request.json").write_text(
            json.dumps(
                {
                    "kind": "add_indicator",
                    "capability": capability,
                    "category": "indicator",
                    "phase": "indicator",
                    "confidence": 0.85,
                    "reasoning": "trend filter",
                    "plugin_slug": PLUGIN_SLUG,
                }
            )
        )
    return s


async def _seed_plugin(
    session,
    *,
    slug: str = PLUGIN_SLUG,
    state: str = PluginState.VERIFIED.value,
    kind: str = "indicators",
) -> Plugin:
    now = datetime.now(UTC)
    p = Plugin(
        slug=slug,
        current_state=state,
        kind=kind,
        spec_path=f"data/plugins/{slug}/v1/spec.md",
        contract_path=f"data/plugins/{slug}/v1/contract.yaml",
        created_at=now,
        updated_at=now,
    )
    session.add(p)
    await session.commit()
    await session.refresh(p)
    return p


async def _seed_author_agent_run_for_plugin(
    session,
    settings,
    *,
    plugin_id: int,
    parent_strategy_id: int,
    capability: str,
) -> str:
    """Mirror what PluginAuthor.run_fresh leaves behind: a 'plugin_author'
    AgentRun with `plugin_id` set and `input_artifact_path` pointing at the
    originating sidecar. plugin_flow's capability guard reads this row."""
    src_dir = settings.data_dir / "strategies" / f"_origin_{plugin_id}" / "iteration_001"
    src_dir.mkdir(parents=True, exist_ok=True)
    sidecar_path = src_dir / "add_indicator_request.json"
    sidecar_path.write_text(
        json.dumps(
            {
                "kind": "add_indicator",
                "capability": capability,
                "category": "indicator",
                "phase": "indicator",
                "confidence": 0.9,
                "reasoning": "origin",
            }
        )
    )
    now = datetime.now(UTC)
    # M5d: lookup_plugin_capability now reads from the plugin_planner AR row
    # (the planner has input_artifact_path = sidecar_path + plugin_id linked).
    ar = AgentRun(
        agent_name="plugin_planner",
        status=AgentRunStatus.DONE.value,
        strategy_id=parent_strategy_id,
        plugin_id=plugin_id,
        input_artifact_path=str(sidecar_path),
        started_at=now,
        created_at=now,
        ended_at=now,
    )
    session.add(ar)
    await session.commit()
    return str(sidecar_path)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_post_reiterate_with_plugin_returns_202_and_creates_child(
    client_with_db, monkeypatch
):
    client, session, settings, _ = client_with_db

    parent = await _seed_parent_in_state(
        session, settings, slug="parent_ok", state=StrategyState.BACKTESTED.value,
        with_sidecar=True, capability=DEFAULT_CAPABILITY,
    )
    plugin = await _seed_plugin(session)
    await _seed_author_agent_run_for_plugin(
        session, settings,
        plugin_id=plugin.id, parent_strategy_id=parent.id,
        capability=DEFAULT_CAPABILITY,
    )

    captured: list[tuple] = []

    async def fake_reiter_bg(strategy_id: int, plugin_slug: str, agent_run_id: int) -> None:
        captured.append((strategy_id, plugin_slug, agent_run_id))

    from fwbg_agents.api import plugins as plugins_api
    monkeypatch.setattr(
        plugins_api, "_run_reiterate_with_plugin_background", fake_reiter_bg
    )

    r = await client.post(
        f"/strategies/{parent.id}/reiterate-with-plugin",
        json={"plugin_slug": PLUGIN_SLUG},
    )
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["status"] == "scheduled"
    assert body["strategy_id"] == parent.id
    assert "agent_run_id" in body

    ar = (
        await session.execute(
            select(AgentRun).where(AgentRun.id == body["agent_run_id"])
        )
    ).scalar_one()
    assert ar.agent_name == "translator_reiterate_flow"
    assert ar.status == AgentRunStatus.PENDING.value
    assert ar.strategy_id == parent.id
    assert ar.input_artifact_path is not None
    assert ar.input_artifact_path.endswith("add_indicator_request.json")

    assert captured == [(parent.id, PLUGIN_SLUG, body["agent_run_id"])]


async def test_post_reiterate_with_plugin_404_strategy_missing(client_with_db):
    client, _, _, _ = client_with_db
    r = await client.post(
        "/strategies/99999/reiterate-with-plugin",
        json={"plugin_slug": PLUGIN_SLUG},
    )
    assert r.status_code == 404, r.text
    assert "not found" in r.json()["detail"]


async def test_post_reiterate_with_plugin_422_parent_not_backtested(client_with_db):
    client, session, settings, _ = client_with_db
    parent = await _seed_parent_in_state(
        session, settings, slug="parent_proposed",
        state=StrategyState.PROPOSED.value, with_sidecar=True,
    )
    await _seed_plugin(session)
    r = await client.post(
        f"/strategies/{parent.id}/reiterate-with-plugin",
        json={"plugin_slug": PLUGIN_SLUG},
    )
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert "BACKTESTED" in detail
    assert "proposed" in detail or StrategyState.PROPOSED.value in detail


async def test_post_reiterate_with_plugin_422_plugin_not_verified(client_with_db):
    client, session, settings, _ = client_with_db
    parent = await _seed_parent_in_state(
        session, settings, slug="parent_pluginauth",
        state=StrategyState.BACKTESTED.value, with_sidecar=True,
    )
    await _seed_plugin(session, state=PluginState.AUTHORED.value)
    r = await client.post(
        f"/strategies/{parent.id}/reiterate-with-plugin",
        json={"plugin_slug": PLUGIN_SLUG},
    )
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert "VERIFIED" in detail


async def test_post_reiterate_with_plugin_422_no_sidecar(client_with_db):
    client, session, settings, _ = client_with_db
    parent = await _seed_parent_in_state(
        session, settings, slug="parent_nosidecar",
        state=StrategyState.BACKTESTED.value, with_sidecar=False,
    )
    plugin = await _seed_plugin(session)
    await _seed_author_agent_run_for_plugin(
        session, settings, plugin_id=plugin.id,
        parent_strategy_id=parent.id, capability=DEFAULT_CAPABILITY,
    )
    r = await client.post(
        f"/strategies/{parent.id}/reiterate-with-plugin",
        json={"plugin_slug": PLUGIN_SLUG},
    )
    assert r.status_code == 422, r.text
    assert "add_indicator_request" in r.json()["detail"]


async def test_post_reiterate_with_plugin_422_capability_mismatch(client_with_db):
    client, session, settings, _ = client_with_db
    parent = await _seed_parent_in_state(
        session, settings, slug="parent_capmismatch",
        state=StrategyState.BACKTESTED.value, with_sidecar=True,
        capability="rolling close-price mean",
    )
    plugin = await _seed_plugin(session)
    await _seed_author_agent_run_for_plugin(
        session, settings, plugin_id=plugin.id,
        parent_strategy_id=parent.id,
        capability="detect strong trends",  # ← different from parent sidecar
    )
    r = await client.post(
        f"/strategies/{parent.id}/reiterate-with-plugin",
        json={"plugin_slug": PLUGIN_SLUG},
    )
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert "capability" in detail.lower()
    assert "match" in detail.lower() or "mismatch" in detail.lower()
