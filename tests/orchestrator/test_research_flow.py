"""Tests for `orchestrator/research_flow.py` — Researcher→Translator glue (M4)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from fwbg_agents.agents.researcher import ResearcherInput
from fwbg_agents.orchestrator.lifecycle import strategy_dir
from fwbg_agents.orchestrator.research_flow import (
    ReiteratePreconditionError,
    reiterate,
    research_and_translate,
)
from fwbg_agents.persistence.database import Base
from fwbg_agents.persistence.models import (
    AgentRun,
    AgentRunStatus,
    Strategy,
    StrategyState,
    StrategyTag,
    Transition,
)


_HYP_ARGS = {
    "title": "ORB on FOREX majors",
    "asset_class": "FOREX",
    "strategy_family": "ORB",
    "hypothesis": "Opening range breakouts on EURUSD M15 produce a momentum edge.",
    "expected_edge_explanation": "Early London liquidity creates session persistence.",
    "key_indicators": ["opening_range", "atr"],
    "tags": ["orb", "intraday", "forex_majors"],
    "sources": [
        {"url": "https://example.com/orb", "title": "ORB note",
         "why_relevant": "documents the London-open ORB effect"},
    ],
    "differentiates_from": [],
}


_STRATEGY_JSON = {
    "name": "will_be_overwritten",
    "description": "ORB rule-based on FOREX majors",
    "hypothesis": "Opening range breakouts on EURUSD M15 produce a momentum edge.",
    "expected_outcome": "sharpe > 1.0 with PBO < 0.5",
    "datasource": "forexsb",
    "pipeline": "orb_simple_v1",
    "model": "signal_orb_v1",
    "filters": "orb_scalping_v1",
    "validation": "walk_forward_intraday_v1",
    "resources": "standard_v1",
    "timeframe": "MINUTE_15",
    "exit_strategies": [
        {"name": "orb_based",
         "params": {"sl_mult": 1.0, "tp_mult": 5.0, "atr_period": 14}},
    ],
    "tags": ["orb", "intraday", "forex_majors"],
    "optimization": {},
}


def _dispatch_model() -> FunctionModel:
    """One FunctionModel that serves both agents. The handler peeks at the
    registered output tool's schema — Translator's has a `pipeline` field
    while Researcher's has `differentiates_from` — and emits the matching
    canned payload."""

    def handler(_messages, info: AgentInfo) -> ModelResponse:
        schema = {}
        tools = list(info.output_tools or [])
        if tools:
            schema = getattr(tools[0], "parameters_json_schema", {}) or {}
        props = schema.get("properties", {})
        if "pipeline" in props:
            return ModelResponse(parts=[ToolCallPart("final_result", _STRATEGY_JSON)])
        return ModelResponse(parts=[ToolCallPart("final_result", _HYP_ARGS)])

    return FunctionModel(handler)


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    from fwbg_agents.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/research_flow.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as session:
        yield session, tmp_path
    await engine.dispose()


@pytest.mark.asyncio
async def test_research_and_translate_persists_strategy_and_artifacts(db):
    session, _ = db
    model = _dispatch_model()

    strategy_id = await research_and_translate(
        session, ResearcherInput(asset_class="FOREX"), model=model, tavily=None
    )

    s = (await session.execute(select(Strategy).where(Strategy.id == strategy_id))).scalar_one()
    assert s.slug == "orb__forex__001"
    assert s.current_state == StrategyState.PROPOSED.value
    assert s.iteration_count == 1
    assert s.asset_class == "FOREX"
    assert s.strategy_family == "ORB"

    # Tags persisted
    tags = (
        await session.execute(select(StrategyTag.tag).where(StrategyTag.strategy_id == s.id))
    ).scalars().all()
    assert set(tags) == {"orb", "intraday", "forex_majors"}

    # Initial Transition emitted
    transitions = (
        await session.execute(
            select(Transition).where(
                (Transition.entity_type == "strategy") & (Transition.entity_id == s.id)
            )
        )
    ).scalars().all()
    assert len(transitions) == 1
    assert transitions[0].from_state is None
    assert transitions[0].to_state == StrategyState.PROPOSED.value
    assert transitions[0].payload["hypothesis_title"] == _HYP_ARGS["title"]

    # Artifacts written
    it_dir = strategy_dir(s.slug) / "iteration_001"
    assert (it_dir / "hypothesis.json").is_file()
    assert (it_dir / "research_notes.md").is_file()
    assert (it_dir / "strategy.json").is_file()
    assert (it_dir / "spec.md").is_file()

    # hypothesis_path + spec_path set on Strategy
    assert s.hypothesis_path == str(it_dir / "hypothesis.json")
    assert s.spec_path == str(it_dir / "spec.md")

    # strategy.json has canonical slug
    strat = json.loads((it_dir / "strategy.json").read_text())
    assert strat["name"] == s.slug

    # Two AgentRun rows: researcher + translator, both DONE
    runs = (await session.execute(select(AgentRun).order_by(AgentRun.id))).scalars().all()
    assert [r.agent_name for r in runs] == ["researcher", "translator"]
    assert all(r.status == AgentRunStatus.DONE.value for r in runs)


@pytest.mark.asyncio
async def test_research_notes_render_includes_sources_and_diffs(db):
    session, _ = db
    model = _dispatch_model()
    sid = await research_and_translate(
        session, ResearcherInput(asset_class="FOREX"), model=model, tavily=None
    )
    s = (await session.execute(select(Strategy).where(Strategy.id == sid))).scalar_one()
    notes = (strategy_dir(s.slug) / "iteration_001" / "research_notes.md").read_text()
    assert "ORB note" in notes  # source title
    assert "no prior art surfaced" in notes  # empty differentiates_from rendering
    assert "`opening_range`" in notes


@pytest.mark.asyncio
async def test_reiterate_rejects_when_parent_not_backtested(db):
    session, _ = db
    now = datetime.now(UTC)
    parent = Strategy(
        slug="orb__forex__001",
        current_state=StrategyState.PROPOSED.value,  # not BACKTESTED
        iteration_count=1,
        asset_class="FOREX",
        strategy_family="ORB",
        created_at=now,
        updated_at=now,
    )
    session.add(parent)
    await session.commit()
    await session.refresh(parent)

    with pytest.raises(ReiteratePreconditionError, match="BACKTESTED"):
        await reiterate(session, parent.id, model=_dispatch_model())


@pytest.mark.asyncio
async def test_reiterate_rejects_when_sidecar_missing(db):
    session, _ = db
    now = datetime.now(UTC)
    parent = Strategy(
        slug="orb__forex__001",
        current_state=StrategyState.BACKTESTED.value,
        iteration_count=1,
        asset_class="FOREX",
        strategy_family="ORB",
        created_at=now,
        updated_at=now,
    )
    session.add(parent)
    await session.commit()
    await session.refresh(parent)

    with pytest.raises(ReiteratePreconditionError, match="analyst_recommendation"):
        await reiterate(session, parent.id, model=_dispatch_model())


@pytest.mark.asyncio
async def test_reiterate_returns_child_id_when_preconditions_met(db):
    session, _ = db
    now = datetime.now(UTC)
    parent = Strategy(
        slug="orb__forex__001",
        current_state=StrategyState.BACKTESTED.value,
        iteration_count=1,
        asset_class="FOREX",
        strategy_family="ORB",
        created_at=now,
        updated_at=now,
    )
    session.add(parent)
    await session.commit()
    await session.refresh(parent)

    # Pre-stage parent iteration_001 with strategy.json + sidecar.
    parent_dir = strategy_dir(parent.slug) / "iteration_001"
    parent_dir.mkdir(parents=True, exist_ok=True)
    (parent_dir / "strategy.json").write_text(json.dumps(_STRATEGY_JSON, indent=2))
    (parent_dir / "analyst_recommendation.json").write_text(
        json.dumps({
            "kind": "tune_params",
            "confidence": "high",
            "reasoning": "narrow grid around best fold",
            "param": "atr_period",
            "new_range": [10, 12, 14, 16],
        })
    )

    child_id = await reiterate(session, parent.id, model=_dispatch_model())
    assert child_id != parent.id

    child = (await session.execute(select(Strategy).where(Strategy.id == child_id))).scalar_one()
    assert child.parent_strategy_id == parent.id
    assert child.slug == "orb__forex__002"
    assert child.current_state == StrategyState.PROPOSED.value
