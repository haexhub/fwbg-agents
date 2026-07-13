"""Lineage helpers — generation depth + family history rendering for the Analyst."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest_asyncio
import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from fwbg_agents.orchestrator.lineage import (
    family_strategies,
    generation_depth,
    has_metric_improvement,
    render_family_history,
)
from fwbg_agents.persistence.database import Base
from fwbg_agents.persistence.models import (
    Strategy,
    StrategyState,
    Transition,
)


@pytest_asyncio.fixture
async def chain(tmp_path, monkeypatch):
    """Root (abandoned) → child (backtested) chain with on-disk artifacts."""
    from fwbg_agents.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/lineage.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async with Session() as s:
        now = datetime.now(UTC)
        root = Strategy(
            slug="fam_root",
            current_state=StrategyState.BACKTESTED.value,
            iteration_count=1,
            asset_class="FOREX",
            strategy_family="ORB",
            created_at=now,
            updated_at=now,
        )
        s.add(root)
        await s.flush()
        child = Strategy(
            slug="fam_child",
            current_state=StrategyState.BACKTESTED.value,
            iteration_count=1,
            parent_strategy_id=root.id,
            asset_class="FOREX",
            strategy_family="ORB",
            created_at=now,
            updated_at=now,
        )
        s.add(child)
        await s.flush()
        s.add(
            Transition(
                entity_type="strategy",
                entity_id=child.id,
                from_state=None,
                to_state=StrategyState.PROPOSED.value,
                reason="translator: re-iterate from fam_root (tune_params)",
                payload={
                    "parent_strategy_id": root.id,
                    "recommendation_kind": "tune_params",
                    "recommendation": {
                        "kind": "tune_params",
                        "params": [{"param": "sl_mult", "new_range": [1.5, 2.0]}],
                    },
                },
                created_by="translator",
                created_at=now,
            )
        )
        await s.commit()
        root_id, child_id = root.id, child.id

    for slug, sharpe in (("fam_root", 0.4), ("fam_child", 1.1)):
        it_dir = settings.data_dir / "strategies" / slug / "iteration_001"
        it_dir.mkdir(parents=True, exist_ok=True)
        (it_dir / "fwbg_results.json").write_text(
            json.dumps(
                {
                    "assets": {
                        "EURUSD": {"unified_metrics": {"sharpe": sharpe, "total_trades": 120}},
                        "GBPUSD": {"unified_metrics": {"sharpe": -0.2, "total_trades": 80}},
                    }
                }
            )
        )
    # The root's analyst verdict (what led to the child).
    (
        settings.data_dir
        / "strategies"
        / "fam_root"
        / "iteration_001"
        / "analyst_recommendation.json"
    ).write_text(
        json.dumps(
            {"kind": "tune_params", "params": [{"param": "sl_mult", "new_range": [1.5, 2.0]}]}
        )
    )

    yield Session, root_id, child_id
    await engine.dispose()


async def test_generation_depth(chain):
    Session, root_id, child_id = chain
    async with Session() as s:
        root = (await s.execute(select(Strategy).where(Strategy.id == root_id))).scalar_one()
        child = (await s.execute(select(Strategy).where(Strategy.id == child_id))).scalar_one()
        assert await generation_depth(s, root) == 1
        assert await generation_depth(s, child) == 2


async def test_family_strategies_from_any_member(chain):
    Session, root_id, child_id = chain
    async with Session() as s:
        child = (await s.execute(select(Strategy).where(Strategy.id == child_id))).scalar_one()
        members = await family_strategies(s, child)
        assert [m.id for m in members] == [root_id, child_id]


async def test_render_family_history(chain, tmp_path):
    from fwbg_agents.config import settings

    Session, _root_id, child_id = chain
    # Abandon post-mortem on the root adds lessons to the history.
    pm = settings.data_dir / "strategies" / "fam_root" / "post_mortem.yaml"
    pm.write_text(yaml.safe_dump({"lessons": ["GBPUSD never carried the edge"]}))

    async with Session() as s:
        child = (await s.execute(select(Strategy).where(Strategy.id == child_id))).scalar_one()
        depth, history = await render_family_history(s, child)

    assert depth == 2
    assert "`fam_root`" in history
    assert "`fam_child` [backtested] ← CURRENT" in history
    # The change that created the child (from its creation Transition).
    assert "change applied vs parent: tune_params" in history
    assert "sl_mult" in history
    # Per-asset metrics of both generations.
    assert "EURUSD(sharpe=0.4" in history
    assert "EURUSD(sharpe=1.1" in history
    # Root's own analyst verdict + abandon lesson.
    assert "analyst verdict: tune_params" in history
    assert "lesson: GBPUSD never carried the edge" in history


async def test_render_family_history_includes_per_asset_series(chain):
    """WP3: the history shows each asset's Sharpe trend across the chain so the
    Analyst can spot consistently-lagging assets to narrow away."""
    Session, _root_id, child_id = chain
    async with Session() as s:
        child = (await s.execute(select(Strategy).where(Strategy.id == child_id))).scalar_one()
        _depth, history = await render_family_history(s, child)

    assert "per-asset Sharpe across the chain" in history
    assert "EURUSD: sharpe 0.40 → 1.10" in history
    assert "GBPUSD: sharpe -0.20 → -0.20" in history


async def test_render_family_history_first_iteration(chain):
    Session, _root_id, _child_id = chain
    async with Session() as s:
        # A strategy without relatives gets the placeholder instead of the
        # full family block.
        now = datetime.now(UTC)
        lone = Strategy(
            slug="lone_wolf",
            current_state=StrategyState.BACKTESTED.value,
            iteration_count=1,
            asset_class="FOREX",
            strategy_family="ORB",
            created_at=now,
            updated_at=now,
        )
        s.add(lone)
        await s.commit()
        depth, history = await render_family_history(s, lone)

    assert depth == 1
    assert history == "(first iteration — no prior family history)"


# ---------------------------------------------------------------------------
# has_metric_improvement
# ---------------------------------------------------------------------------


def test_has_metric_improvement_rising_sharpe():
    history = [{"sharpe": 0.5}, {"sharpe": 0.8}, {"sharpe": 1.1}]
    assert has_metric_improvement(history) is True


def test_has_metric_improvement_rising_profit_factor():
    history = [{"profit_factor": 1.0}, {"profit_factor": 1.2}, {"profit_factor": 1.5}]
    assert has_metric_improvement(history) is True


def test_has_metric_improvement_falling():
    history = [{"sharpe": 1.5}, {"sharpe": 1.0}, {"sharpe": 0.5}]
    assert has_metric_improvement(history) is False


def test_has_metric_improvement_flat():
    history = [{"sharpe": 1.0}, {"sharpe": 1.0}, {"sharpe": 1.0}]
    assert has_metric_improvement(history) is False


def test_has_metric_improvement_single_entry():
    assert has_metric_improvement([{"sharpe": 2.0}]) is False


def test_has_metric_improvement_empty():
    assert has_metric_improvement([]) is False


def test_has_metric_improvement_fewer_entries_than_lookback():
    # Only 2 entries, lookback=3 — still works with what's there.
    history = [{"sharpe": 0.3}, {"sharpe": 0.9}]
    assert has_metric_improvement(history, lookback=3) is True


def test_has_metric_improvement_missing_metrics():
    # Neither key present in any entry → no numeric values → False.
    history = [{"win_rate": 0.4}, {"win_rate": 0.6}, {"win_rate": 0.8}]
    assert has_metric_improvement(history) is False


def test_has_metric_improvement_partial_missing():
    # sharpe missing in some entries; profit_factor present and rising.
    history = [{"profit_factor": 1.0}, {"win_rate": 0.5}, {"profit_factor": 1.4}]
    assert has_metric_improvement(history) is True


def test_has_metric_improvement_uses_lookback_window():
    # Oldest entries show improvement but the recent window (last 2) is flat.
    history = [
        {"sharpe": 0.1},
        {"sharpe": 0.9},
        {"sharpe": 1.0},
        {"sharpe": 1.0},
    ]
    assert has_metric_improvement(history, lookback=2) is False
    assert has_metric_improvement(history, lookback=4) is True
