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
    AddIndicator,
    Analyst,
    ChangeExit,
    Promote,
    TuneParams,
    _render_catalog_snapshot,
)
from fwbg_agents.orchestrator.plugin_catalog import (
    PluginCatalog,
    PluginManifest,
    _load_fwbg_cached,
)
from fwbg_agents.persistence.database import Base
from fwbg_agents.persistence.models import (
    AgentRun,
    AgentRunStatus,
    LlmCall,
    Strategy,
    StrategyState,
)


def _stub_model(tool_name: str, args: dict) -> FunctionModel:
    """Return a FunctionModel that always emits one final-result tool call."""

    def handler(_messages, _info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[ToolCallPart(tool_name, args)])

    return FunctionModel(handler)


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
        {
            "kind": "promote",
            "confidence": 0.9,
            "reasoning": "metrics clear the criteria across the board",
        },
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


async def test_analyst_returns_add_indicator(db_and_backtested, monkeypatch, tmp_path):
    SessionMaker, strategy_id, _ = db_and_backtested
    # Point catalog at an empty fwbg root so the rendered prompt's snapshot is
    # empty-ish — keeps the test independent of the host's fwbg checkout.
    _load_fwbg_cached.cache_clear()
    from fwbg_agents.config import settings as _settings
    monkeypatch.setattr(_settings, "fwbg_repo_root", tmp_path / "no-fwbg")

    test_model = _stub_model(
        "final_result_AddIndicator",
        {
            "kind": "add_indicator",
            "confidence": 0.7,
            "reasoning": "no zone-pivot plugin in catalog; strategy needs support/resistance bands",
            "phase": "indicators",
            "capability": "support/resistance zones from pivot points",
            "category": "indicator",
        },
    )
    async with SessionMaker() as session:
        s = (await session.execute(select(Strategy).where(Strategy.id == strategy_id))).scalar_one()
        rec = await Analyst(session, model=test_model).analyze(s)

    assert isinstance(rec, AddIndicator)
    assert rec.phase == "indicators"
    assert rec.category == "indicator"
    assert "pivot" in rec.capability


def test_render_catalog_snapshot_lists_categories_and_slugs():
    cat = PluginCatalog(by_category={
        "indicators": {
            slug: PluginManifest(
                name=slug, category="indicators", provenance="fwbg-core",
                version="1.0.0", source_path=Path("/tmp/x"),
            ) for slug in ["ema", "sma"]
        },
        "exit_strategies": {
            "fixed": PluginManifest(
                name="fixed", category="exit_strategies", provenance="fwbg-core",
                version="1.0.0", source_path=Path("/tmp/x"),
            ),
        },
    })
    snap = _render_catalog_snapshot(cat)
    # Category labels are normalised to the AddIndicator.category enum spelling
    # (singular) so the model copies a valid category token.
    assert "indicator: ema, sma" in snap
    assert "exit_strategy: fixed" in snap
    assert "indicators:" not in snap  # plural key must not leak into the snapshot


def test_render_catalog_snapshot_empty():
    snap = _render_catalog_snapshot(PluginCatalog(by_category={}))
    assert "catalog empty" in snap.lower()


def test_add_indicator_coerces_plural_category_and_bad_phase():
    """The model copies plural category keys ('indicators') from the snapshot and
    invents phases the prompt never lists ('entry'). Both used to exhaust
    pydantic-ai's output retries and crash the analyst — the before-validator now
    degrades them to the closest valid enum member instead."""
    rec = AddIndicator(
        confidence=0.7,
        reasoning="needs pivot zones",
        phase="entry",
        capability="support/resistance zones",
        category="indicators",
    )
    assert rec.category == "indicator"
    assert rec.phase == "indicators"


async def test_analyst_add_indicator_survives_invalid_enums(
    db_and_backtested, monkeypatch, tmp_path
):
    """End-to-end regression for the production crash: a model emitting the
    plural category + bogus phase must yield a normalised AddIndicator and a
    DONE AgentRun rather than 'Exceeded maximum output retries (3)'."""
    SessionMaker, strategy_id, _ = db_and_backtested
    _load_fwbg_cached.cache_clear()
    from fwbg_agents.config import settings as _settings
    monkeypatch.setattr(_settings, "fwbg_repo_root", tmp_path / "no-fwbg")

    test_model = _stub_model(
        "final_result_AddIndicator",
        {
            "kind": "add_indicator",
            "confidence": 0.7,
            "reasoning": "no pivot-zone plugin in catalog",
            "phase": "entry",  # not a valid phase — model hallucination
            "capability": "support/resistance zones from pivot points",
            "category": "indicators",  # plural — copied from the catalog snapshot
        },
    )
    async with SessionMaker() as session:
        s = (await session.execute(select(Strategy).where(Strategy.id == strategy_id))).scalar_one()
        rec = await Analyst(session, model=test_model).analyze(s)

    assert isinstance(rec, AddIndicator)
    assert rec.category == "indicator"
    assert rec.phase == "indicators"

    async with SessionMaker() as v:
        ar = (await v.execute(select(AgentRun))).scalars().all()
        assert len(ar) == 1
        assert ar[0].status == AgentRunStatus.DONE.value


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
