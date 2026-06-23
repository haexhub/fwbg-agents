"""Analyst agent tests.

The Analyst is the first LLM consumer in fwbg-agents. We use pydantic-ai's
TestModel to inject canned structured responses — no real LLM is called.

Recommendation variants tested:
- Promote
- Abandon (must include post_mortem_summary + lessons)
- TuneParams
- ChangeExit

Plus: missing fwbg_results.json → AgentRun status=failed + exception bubbles.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
import pytest_asyncio
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from fwbg_agents.agents.analyst import (
    Abandon,
    Analyst,
    ChangeExit,
    Promote,
    TuneParams,
)


def _stub_model(tool_name: str, args: dict) -> FunctionModel:
    """Return a FunctionModel that always emits one final-result tool call."""

    def handler(_messages, _info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[ToolCallPart(tool_name, args)])

    return FunctionModel(handler)
from fwbg_agents.persistence.database import Base
from fwbg_agents.persistence.models import (
    AgentRun,
    AgentRunStatus,
    LlmCall,
    Strategy,
    StrategyState,
)


@pytest_asyncio.fixture
async def db_and_backtested(tmp_path, monkeypatch):
    from fwbg_agents.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path / "agents_data")

    db_url = f"sqlite+aiosqlite:///{tmp_path}/analyst.db"
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
        strategy_id = s.id

    it_dir = settings.data_dir / "strategies" / "demo_v1" / "iteration_001"
    it_dir.mkdir(parents=True, exist_ok=True)
    (it_dir / "strategy.json").write_text(json.dumps({"name": "demo_v1"}))
    (it_dir / "fwbg_results.json").write_text(
        json.dumps(
            {
                "run_id": "abc",
                "status": "completed",
                "assets": {
                    "EURUSD": {
                        "unified_metrics": {
                            "sharpe": 1.8,
                            "profit_factor": 1.7,
                            "trades": 400,
                            "mc_pvalue": 0.03,
                            "max_drawdown": 0.18,
                        }
                    }
                },
            }
        )
    )

    yield Session, strategy_id, tmp_path
    await engine.dispose()


async def test_analyst_returns_promote(db_and_backtested):
    SessionMaker, strategy_id, _ = db_and_backtested
    test_model = _stub_model(
        "final_result_Promote",
        {"kind": "promote", "confidence": 0.9, "reasoning": "metrics clear the criteria across the board"},
    )
    async with SessionMaker() as session:
        s = (await session.execute(select(Strategy).where(Strategy.id == strategy_id))).scalar_one()
        analyst = Analyst(session, model=test_model)
        rec = await analyst.analyze(s)

    assert isinstance(rec, Promote)
    assert rec.confidence == 0.9
    assert "criteria" in rec.reasoning

    async with SessionMaker() as v:
        ar = (await v.execute(select(AgentRun))).scalars().all()
        assert len(ar) == 1
        assert ar[0].status == AgentRunStatus.DONE.value
        assert ar[0].agent_name == "analyst"
        calls = (await v.execute(select(LlmCall))).scalars().all()
        assert len(calls) == 1
        assert calls[0].agent_run_id == ar[0].id
        assert calls[0].input_tokens >= 0  # FunctionModel may report 0


async def test_analyst_returns_abandon(db_and_backtested):
    SessionMaker, strategy_id, _ = db_and_backtested
    test_model = _stub_model(
        "final_result_Abandon",
        {
            "kind": "abandon",
            "confidence": 0.8,
            "reasoning": "fundamentally unprofitable in this regime",
            "post_mortem_summary": "no edge after costs",
            "lessons": ["RSI mean-reversion on intraday DAX has no statistical edge"],
        },
    )
    async with SessionMaker() as session:
        s = (await session.execute(select(Strategy).where(Strategy.id == strategy_id))).scalar_one()
        analyst = Analyst(session, model=test_model)
        rec = await analyst.analyze(s)

    assert isinstance(rec, Abandon)
    assert "no edge" in rec.post_mortem_summary
    assert len(rec.lessons) == 1


async def test_analyst_returns_tune_params(db_and_backtested):
    SessionMaker, strategy_id, _ = db_and_backtested
    test_model = _stub_model(
        "final_result_TuneParams",
        {
            "kind": "tune_params",
            "confidence": 0.6,
            "reasoning": "tp too tight on M15",
            "param": "tp_multiplier",
            "new_range": [1.5, 2.0, 2.5],
        },
    )
    async with SessionMaker() as session:
        s = (await session.execute(select(Strategy).where(Strategy.id == strategy_id))).scalar_one()
        rec = await Analyst(session, model=test_model).analyze(s)

    assert isinstance(rec, TuneParams)
    assert rec.param == "tp_multiplier"
    assert rec.new_range == [1.5, 2.0, 2.5]


async def test_analyst_returns_change_exit(db_and_backtested):
    SessionMaker, strategy_id, _ = db_and_backtested
    test_model = _stub_model(
        "final_result_ChangeExit",
        {
            "kind": "change_exit",
            "confidence": 0.5,
            "reasoning": "static SL gets stopped on noise",
            "from_exit": "static_sl",
            "to_exit": "atr_trailing_sl",
        },
    )
    async with SessionMaker() as session:
        s = (await session.execute(select(Strategy).where(Strategy.id == strategy_id))).scalar_one()
        rec = await Analyst(session, model=test_model).analyze(s)

    assert isinstance(rec, ChangeExit)
    assert rec.from_exit == "static_sl"
    assert rec.to_exit == "atr_trailing_sl"


async def test_analyst_missing_results_marks_agent_run_failed(db_and_backtested):
    SessionMaker, strategy_id, _ = db_and_backtested
    from fwbg_agents.config import settings as cfg

    results = cfg.data_dir / "strategies" / "demo_v1" / "iteration_001" / "fwbg_results.json"
    results.unlink()

    test_model = _stub_model(
        "final_result_Promote", {"kind": "promote", "confidence": 1.0, "reasoning": "x"}
    )
    async with SessionMaker() as session:
        s = (await session.execute(select(Strategy).where(Strategy.id == strategy_id))).scalar_one()
        with pytest.raises(FileNotFoundError):
            await Analyst(session, model=test_model).analyze(s)

    async with SessionMaker() as v:
        ar = (await v.execute(select(AgentRun))).scalars().all()
        assert len(ar) == 1
        assert ar[0].status == AgentRunStatus.FAILED.value
