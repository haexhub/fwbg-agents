"""Recommendation validator tests.

The validator is the hard rule between the Analyst's LLM output and any
actual state change. The LLM can request anything; only the validator
decides what actually happens.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
import pytest_asyncio
import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from fwbg_agents.agents.analyst import Abandon, ChangeExit, Promote, TuneParams
from fwbg_agents.orchestrator.lifecycle import InvalidTransition
from fwbg_agents.orchestrator.recommendations import validate_and_apply
from fwbg_agents.persistence.database import Base
from fwbg_agents.persistence.models import (
    Strategy,
    StrategyState,
    Transition,
)


@pytest_asyncio.fixture
async def db_and_backtested(tmp_path, monkeypatch):
    """A strategy already in BACKTESTED with an iteration_001 dir on disk."""
    from fwbg_agents.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")

    db_url = f"sqlite+aiosqlite:///{tmp_path}/recs.db"
    engine = create_async_engine(db_url, future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async with Session() as setup:
        now = datetime.now(UTC)
        s = Strategy(
            slug="demo_v1",
            current_state=StrategyState.BACKTESTED.value,
            iteration_count=0,
            asset_class="FOREX",
            strategy_family="ORB",
            created_at=now,
            updated_at=now,
        )
        setup.add(s)
        await setup.commit()
        await setup.refresh(s)
        sid = s.id

    it_dir = settings.data_dir / "strategies" / "demo_v1" / "iteration_001"
    it_dir.mkdir(parents=True, exist_ok=True)
    yield Session, sid, tmp_path, settings.criteria_dir
    await engine.dispose()


def _write_passing_criteria(criteria_dir: Path):
    criteria_dir.mkdir(parents=True, exist_ok=True)
    (criteria_dir / "FOREX.yaml").write_text(
        yaml.safe_dump(
            {
                "backtest_to_paper": {
                    "required_all": [
                        {"sharpe": ">= 1.5"},
                        {"profit_factor": ">= 1.6"},
                        {"trades": ">= 300"},
                    ],
                    "hard_blockers": [{"mc_pvalue": "<= 0.05"}],
                }
            }
        )
    )


def _write_strict_criteria(criteria_dir: Path):
    criteria_dir.mkdir(parents=True, exist_ok=True)
    (criteria_dir / "FOREX.yaml").write_text(
        yaml.safe_dump({"backtest_to_paper": {"required_all": [{"sharpe": ">= 3.5"}]}})
    )


_GOOD_METRICS = {"sharpe": 2.0, "profit_factor": 1.8, "trades": 500, "mc_pvalue": 0.02}
_BAD_METRICS = {"sharpe": 1.0, "profit_factor": 1.1, "trades": 50, "mc_pvalue": 0.2}


async def test_promote_with_passing_metrics_transitions_to_paper(db_and_backtested):
    SessionMaker, sid, _tmp, criteria_dir = db_and_backtested
    _write_passing_criteria(criteria_dir)

    async with SessionMaker() as session:
        s = (await session.execute(select(Strategy).where(Strategy.id == sid))).scalar_one()
        rec = Promote(confidence=0.9, reasoning="all gates clear")
        tr = await validate_and_apply(session, s, rec, metrics=_GOOD_METRICS)
        assert tr is not None
        assert tr.to_state == StrategyState.PAPER_TRADING.value
        assert tr.payload["recommendation"]["kind"] == "promote"
        assert tr.payload["backtest_metrics"]["sharpe"] == 2.0

    async with SessionMaker() as v:
        s = (await v.execute(select(Strategy).where(Strategy.id == sid))).scalar_one()
        assert s.current_state == StrategyState.PAPER_TRADING.value


async def test_promote_with_failing_metrics_is_rejected(db_and_backtested):
    SessionMaker, sid, _tmp, criteria_dir = db_and_backtested
    _write_strict_criteria(criteria_dir)  # sharpe >= 3.5 — 2.0 fails

    async with SessionMaker() as session:
        s = (await session.execute(select(Strategy).where(Strategy.id == sid))).scalar_one()
        rec = Promote(confidence=1.0, reasoning="LLM thinks it's great anyway")
        with pytest.raises(InvalidTransition):
            await validate_and_apply(session, s, rec, metrics=_GOOD_METRICS)

    async with SessionMaker() as v:
        s = (await v.execute(select(Strategy).where(Strategy.id == sid))).scalar_one()
        assert s.current_state == StrategyState.BACKTESTED.value  # unchanged
        assert (await v.execute(select(Transition))).scalars().all() == []


async def test_abandon_writes_post_mortem_and_transitions(db_and_backtested):
    SessionMaker, sid, tmp_path, _criteria = db_and_backtested

    async with SessionMaker() as session:
        s = (await session.execute(select(Strategy).where(Strategy.id == sid))).scalar_one()
        rec = Abandon(
            confidence=0.85,
            reasoning="no edge",
            post_mortem_summary="failed across regimes",
            lessons=["RSI mean reversion on intraday DAX shows no edge", "Choose trend strategies instead"],
        )
        tr = await validate_and_apply(session, s, rec, metrics=_BAD_METRICS)
        assert tr is not None
        assert tr.to_state == StrategyState.ABANDONED.value

    pm = tmp_path / "data" / "strategies" / "demo_v1" / "post_mortem.yaml"
    assert pm.is_file()
    pm_data = yaml.safe_load(pm.read_text())
    assert pm_data["summary"] == "failed across regimes"
    assert "no edge" in pm_data["lessons"][0].lower() or len(pm_data["lessons"]) == 2

    async with SessionMaker() as v:
        s = (await v.execute(select(Strategy).where(Strategy.id == sid))).scalar_one()
        assert s.current_state == StrategyState.ABANDONED.value
        assert s.post_mortem_path.endswith("post_mortem.yaml")


async def test_tune_params_records_sidecar_no_transition(db_and_backtested):
    SessionMaker, sid, tmp_path, _criteria = db_and_backtested

    async with SessionMaker() as session:
        s = (await session.execute(select(Strategy).where(Strategy.id == sid))).scalar_one()
        rec = TuneParams(
            confidence=0.6, reasoning="tp too tight", param="tp_mult", new_range=[1.5, 2.0, 2.5]
        )
        tr = await validate_and_apply(session, s, rec, metrics=_BAD_METRICS)
        assert tr is None

    sidecar = (
        tmp_path
        / "data"
        / "strategies"
        / "demo_v1"
        / "iteration_001"
        / "analyst_recommendation.json"
    )
    assert sidecar.is_file()
    rec_persisted = json.loads(sidecar.read_text())
    assert rec_persisted["kind"] == "tune_params"
    assert rec_persisted["param"] == "tp_mult"

    async with SessionMaker() as v:
        s = (await v.execute(select(Strategy).where(Strategy.id == sid))).scalar_one()
        assert s.current_state == StrategyState.BACKTESTED.value  # unchanged
        assert (await v.execute(select(Transition))).scalars().all() == []


async def test_change_exit_records_sidecar_no_transition(db_and_backtested):
    SessionMaker, sid, tmp_path, _criteria = db_and_backtested
    async with SessionMaker() as session:
        s = (await session.execute(select(Strategy).where(Strategy.id == sid))).scalar_one()
        rec = ChangeExit(
            confidence=0.5, reasoning="static SL too tight", from_exit="static_sl", to_exit="atr_trailing_sl"
        )
        tr = await validate_and_apply(session, s, rec, metrics=_BAD_METRICS)
        assert tr is None

    sidecar = (
        tmp_path
        / "data"
        / "strategies"
        / "demo_v1"
        / "iteration_001"
        / "analyst_recommendation.json"
    )
    rec_persisted = json.loads(sidecar.read_text())
    assert rec_persisted["kind"] == "change_exit"
    assert rec_persisted["to_exit"] == "atr_trailing_sl"
