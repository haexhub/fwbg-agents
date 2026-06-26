"""Tests for `orchestrator/research_flow.py` — Researcher→Translator glue (M4)."""

from __future__ import annotations

import itertools
import json
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from fwbg_agents.agents.researcher import ResearcherError, ResearcherInput
from fwbg_agents.orchestrator import research_flow
from fwbg_agents.orchestrator.hypotheses import ResearcherHypothesis
from fwbg_agents.orchestrator.lifecycle import strategy_dir
from fwbg_agents.orchestrator.research_flow import (
    ReiteratePreconditionError,
    ResearcherFanoutExhaustedError,
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
    # _generate_valid_hypothesis opens its own SessionLocal-derived session
    # per fan-out candidate (decision C) — point that at the same tmp_path
    # engine the test's own `session` uses, so AgentRun rows committed by
    # candidates are visible through both.
    monkeypatch.setattr(research_flow, "SessionLocal", Session)
    async with Session() as session:
        yield session, tmp_path
    await engine.dispose()


def _make_flaky_researcher_factory(n_fail: int):
    """Class to monkeypatch onto `research_flow.Researcher`: the first
    `n_fail` calls (by start order) raise ResearcherError, the rest succeed
    with `_HYP_ARGS`. Used to simulate fan-out candidates being rejected by
    `validate_hypothesis` without needing a real prior-art conflict."""
    counter = itertools.count()

    class _FlakyResearcher:
        def __init__(self, session, *, model=None, search_client=None):
            self.session = session

        async def run(self, input):
            idx = next(counter)
            now = datetime.now(UTC)
            ar = AgentRun(
                agent_name="researcher",
                status=AgentRunStatus.RUNNING.value,
                started_at=now,
                created_at=now,
            )
            self.session.add(ar)
            await self.session.commit()
            await self.session.refresh(ar)

            if idx < n_fail:
                ar.status = AgentRunStatus.FAILED.value
                ar.ended_at = datetime.now(UTC)
                ar.error = f"candidate {idx} rejected: simulated prior-art conflict"
                await self.session.commit()
                raise ResearcherError(ar.error)

            ar.status = AgentRunStatus.DONE.value
            ar.ended_at = datetime.now(UTC)
            await self.session.commit()
            return ResearcherHypothesis(**_HYP_ARGS)

    return _FlakyResearcher


@pytest.mark.asyncio
async def test_research_and_translate_persists_strategy_and_artifacts(db):
    session, _ = db
    model = _dispatch_model()

    strategy_id = await research_and_translate(
        session, ResearcherInput(asset_class="FOREX"), model=model, search_client=None
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
        session, ResearcherInput(asset_class="FOREX"), model=model, search_client=None
    )
    s = (await session.execute(select(Strategy).where(Strategy.id == sid))).scalar_one()
    notes = (strategy_dir(s.slug) / "iteration_001" / "research_notes.md").read_text()
    assert "ORB note" in notes  # source title
    assert "no prior art surfaced" in notes  # empty differentiates_from rendering
    assert "`opening_range`" in notes


@pytest.mark.asyncio
async def test_fanout_n_equals_1_matches_today(db):
    """Regression guard: fanout_n=1 must behave identically to pre-M4b
    single-candidate research_and_translate — same Strategy fields, same
    two AgentRun rows (researcher + translator), both DONE."""
    session, _ = db
    model = _dispatch_model()

    strategy_id = await research_and_translate(
        session,
        ResearcherInput(asset_class="FOREX"),
        model=model,
        search_client=None,
        fanout_n=1,
    )

    s = (await session.execute(select(Strategy).where(Strategy.id == strategy_id))).scalar_one()
    assert s.slug == "orb__forex__001"
    assert s.current_state == StrategyState.PROPOSED.value
    assert s.asset_class == "FOREX"
    assert s.strategy_family == "ORB"

    runs = (await session.execute(select(AgentRun).order_by(AgentRun.id))).scalars().all()
    assert [r.agent_name for r in runs] == ["researcher", "translator"]
    assert all(r.status == AgentRunStatus.DONE.value for r in runs)


@pytest.mark.asyncio
async def test_fanout_returns_first_valid_candidate(db, monkeypatch):
    session, _ = db
    monkeypatch.setattr(research_flow, "Researcher", _make_flaky_researcher_factory(n_fail=2))

    strategy_id = await research_and_translate(
        session,
        ResearcherInput(asset_class="FOREX"),
        model=_dispatch_model(),
        fanout_n=3,
    )

    s = (await session.execute(select(Strategy).where(Strategy.id == strategy_id))).scalar_one()
    assert s.strategy_family == "ORB"
    assert s.current_state == StrategyState.PROPOSED.value


@pytest.mark.asyncio
async def test_fanout_creates_one_agent_run_per_candidate(db, monkeypatch):
    session, _ = db
    monkeypatch.setattr(research_flow, "Researcher", _make_flaky_researcher_factory(n_fail=2))

    await research_and_translate(
        session,
        ResearcherInput(asset_class="FOREX"),
        model=_dispatch_model(),
        fanout_n=3,
    )

    researcher_runs = (
        await session.execute(select(AgentRun).where(AgentRun.agent_name == "researcher"))
    ).scalars().all()
    statuses = sorted(r.status for r in researcher_runs)
    assert statuses == sorted(
        [AgentRunStatus.FAILED.value, AgentRunStatus.FAILED.value, AgentRunStatus.DONE.value]
    )


@pytest.mark.asyncio
async def test_fanout_all_candidates_fail_raises_with_combined_reasons(db, monkeypatch):
    session, _ = db
    monkeypatch.setattr(research_flow, "Researcher", _make_flaky_researcher_factory(n_fail=3))

    with pytest.raises(ResearcherFanoutExhaustedError) as exc_info:
        await research_and_translate(
            session,
            ResearcherInput(asset_class="FOREX"),
            model=_dispatch_model(),
            fanout_n=3,
        )

    message = str(exc_info.value)
    for idx in range(3):
        assert f"candidate {idx} rejected" in message


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
