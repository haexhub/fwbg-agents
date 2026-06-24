"""Translator agent — reiterate mode (M4).

Deterministic: reads the parent's analyst_recommendation.json sidecar, copies
the parent's strategy.json with the recommendation applied, creates a NEW
child Strategy with parent_strategy_id set. Parent is untouched.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from fwbg_agents.agents.translator import Translator, TranslatorFailed
from fwbg_agents.persistence.database import Base
from fwbg_agents.persistence.models import (
    AgentRun,
    AgentRunStatus,
    Strategy,
    StrategyState,
    StrategyTag,
    Transition,
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
        {"name": "orb_based",
         "params": {"sl_mult": 1.0, "tp_mult": 5.0, "atr_period": 14},
         "ct": [0.5]},
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


@pytest_asyncio.fixture
async def db_with_parent(tmp_path, monkeypatch):
    from fwbg_agents.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path / "agents_data")

    db_url = f"sqlite+aiosqlite:///{tmp_path}/translator_reiter.db"
    engine = create_async_engine(db_url, future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    parent_slug = "orb__forex__001"
    async with Session() as setup:
        now = datetime.now(UTC)
        s = Strategy(
            slug=parent_slug,
            current_state=StrategyState.BACKTESTED.value,
            iteration_count=1,
            asset_class="FOREX",
            strategy_family="ORB",
            created_at=now,
            updated_at=now,
        )
        setup.add(s)
        await setup.flush()
        for tag in PARENT_STRATEGY_JSON["tags"]:
            setup.add(StrategyTag(strategy_id=s.id, tag=tag))
        await setup.commit()
        await setup.refresh(s)
        parent_id = s.id

    it_dir = settings.data_dir / "strategies" / parent_slug / "iteration_001"
    it_dir.mkdir(parents=True, exist_ok=True)
    (it_dir / "strategy.json").write_text(json.dumps(PARENT_STRATEGY_JSON, indent=2))
    (it_dir / "hypothesis.json").write_text(json.dumps(PARENT_HYPOTHESIS, indent=2))

    yield Session, parent_id, parent_slug, it_dir
    await engine.dispose()


def _write_sidecar(it_dir, rec_dict):
    (it_dir / "analyst_recommendation.json").write_text(json.dumps(rec_dict, indent=2))


@pytest.mark.asyncio
async def test_reiterate_tune_params_creates_child_with_mutated_param(db_with_parent):
    SessionMaker, parent_id, parent_slug, it_dir = db_with_parent
    _write_sidecar(
        it_dir,
        {
            "kind": "tune_params",
            "confidence": 0.7,
            "reasoning": "tp too tight",
            "param": "sl_mult",
            "new_range": [1.5, 2.0, 2.5],
        },
    )

    async with SessionMaker() as session:
        parent = (
            await session.execute(select(Strategy).where(Strategy.id == parent_id))
        ).scalar_one()
        child = await Translator(session).run_reiterate(parent)

    async with SessionMaker() as v:
        children = (
            await v.execute(
                select(Strategy).where(Strategy.parent_strategy_id == parent_id)
            )
        ).scalars().all()
        assert len(children) == 1
        ch = children[0]
        assert ch.id == child.id
        assert ch.current_state == StrategyState.PROPOSED.value
        assert ch.iteration_count == 1
        assert ch.asset_class == "FOREX"
        assert ch.strategy_family == "ORB"
        assert ch.slug != parent_slug

        parent_after = (
            await v.execute(select(Strategy).where(Strategy.id == parent_id))
        ).scalar_one()
        assert parent_after.current_state == StrategyState.BACKTESTED.value
        assert parent_after.iteration_count == 1  # parent untouched

        # transition row for child PROPOSED
        ts = (
            await v.execute(select(Transition).where(Transition.entity_id == ch.id))
        ).scalars().all()
        assert len(ts) == 1
        assert ts[0].to_state == StrategyState.PROPOSED.value
        assert ts[0].payload.get("parent_strategy_id") == parent_id
        assert ts[0].payload.get("recommendation_kind") == "tune_params"

        # AgentRun
        ar = (await v.execute(select(AgentRun))).scalars().all()
        assert len(ar) == 1
        assert ar[0].status == AgentRunStatus.DONE.value

    # child strategy.json — sl_mult overridden
    from fwbg_agents.config import settings

    child_path = settings.data_dir / "strategies" / child.slug / "iteration_001" / "strategy.json"
    assert child_path.is_file()
    child_data = json.loads(child_path.read_text())
    assert child_data["name"] == child.slug
    assert child_data["optimization"]["grid_params"]["sl_mult"] == [1.5, 2.0, 2.5]


@pytest.mark.asyncio
async def test_reiterate_change_exit_swaps_exit_strategies(db_with_parent):
    SessionMaker, parent_id, _parent_slug, it_dir = db_with_parent
    new_exit = {
        "name": "atr_trailing_sl",
        "params": {"atr_period": 14, "trail_mult": 1.5, "sl_mult": 1.0},
    }
    _write_sidecar(
        it_dir,
        {
            "kind": "change_exit",
            "confidence": 0.6,
            "reasoning": "static SL gets stopped on noise",
            "from_exit": "orb_based",
            "to_exit": "atr_trailing_sl",
            "new_exit_strategy": new_exit,
        },
    )

    async with SessionMaker() as session:
        parent = (await session.execute(select(Strategy).where(Strategy.id == parent_id))).scalar_one()
        child = await Translator(session).run_reiterate(parent)

    from fwbg_agents.config import settings

    child_path = settings.data_dir / "strategies" / child.slug / "iteration_001" / "strategy.json"
    child_data = json.loads(child_path.read_text())
    assert len(child_data["exit_strategies"]) == 1
    assert child_data["exit_strategies"][0]["name"] == "atr_trailing_sl"


@pytest.mark.asyncio
async def test_reiterate_missing_sidecar_fails(db_with_parent):
    SessionMaker, parent_id, *_ = db_with_parent
    async with SessionMaker() as session:
        parent = (await session.execute(select(Strategy).where(Strategy.id == parent_id))).scalar_one()
        with pytest.raises(TranslatorFailed):
            await Translator(session).run_reiterate(parent)


@pytest.mark.asyncio
async def test_reiterate_invalid_resulting_json_fails(db_with_parent):
    SessionMaker, parent_id, _parent_slug, it_dir = db_with_parent
    _write_sidecar(
        it_dir,
        {
            "kind": "change_exit",
            "confidence": 0.5,
            "reasoning": "swap exit",
            "from_exit": "orb_based",
            "to_exit": "fancy_new_exit",
            "new_exit_strategy": {"name": "fancy_new_exit"},  # missing params -> invalid
        },
    )
    async with SessionMaker() as session:
        parent = (await session.execute(select(Strategy).where(Strategy.id == parent_id))).scalar_one()
        with pytest.raises(TranslatorFailed):
            await Translator(session).run_reiterate(parent)


@pytest.mark.asyncio
async def test_reiterate_unknown_recommendation_kind_fails(db_with_parent):
    SessionMaker, parent_id, _parent_slug, it_dir = db_with_parent
    _write_sidecar(it_dir, {"kind": "weird_thing", "confidence": 0.5})
    async with SessionMaker() as session:
        parent = (await session.execute(select(Strategy).where(Strategy.id == parent_id))).scalar_one()
        with pytest.raises(TranslatorFailed):
            await Translator(session).run_reiterate(parent)


@pytest.mark.asyncio
async def test_reiterate_copies_hypothesis_into_child_dir(db_with_parent):
    """Child inherits parent's hypothesis.json for lineage."""
    SessionMaker, parent_id, _parent_slug, it_dir = db_with_parent
    _write_sidecar(
        it_dir,
        {
            "kind": "tune_params",
            "confidence": 0.7,
            "reasoning": "...",
            "param": "sl_mult",
            "new_range": [1.5, 2.0, 2.5],
        },
    )
    async with SessionMaker() as session:
        parent = (await session.execute(select(Strategy).where(Strategy.id == parent_id))).scalar_one()
        child = await Translator(session).run_reiterate(parent)

    from fwbg_agents.config import settings

    child_hyp = settings.data_dir / "strategies" / child.slug / "iteration_001" / "hypothesis.json"
    assert child_hyp.is_file()
    data = json.loads(child_hyp.read_text())
    assert data["strategy_family"] == "ORB"
