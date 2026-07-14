"""Critic agent tests (Plan 010 WP3)."""

from __future__ import annotations

import pytest
import pytest_asyncio
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from fwbg_agents.agents.critic import Critic, CriticReport
from fwbg_agents.orchestrator.hypotheses import ResearcherHypothesis
from fwbg_agents.persistence.database import Base
from fwbg_agents.persistence.models import AgentRun, AgentRunStatus, LlmCall


def _hyp(title: str, **over) -> ResearcherHypothesis:
    base = dict(
        title=title,
        asset_class="FOREX",
        strategy_family="mean_reversion",
        edge_mechanism="RSI extremes after London-open overreactions mean-revert",
        hypothesis="During London, EUR/USD mean-reverts after 1-bar momentum spikes.",
        expected_edge_explanation="Liquidity-driven overreactions revert as US algos take over.",
        key_indicators=["rsi", "atr"],
        tags=["mean_reversion", "intraday"],
        sources=[{"url": "https://example.com/a", "title": "x", "why_relevant": "y"}],
        suggested_universe=[{"scope": "asset_class", "value": "FOREX", "rationale": "majors"}],
        differentiates_from=[],
    )
    base.update(over)
    return ResearcherHypothesis(**base)


def _stub_model(args: dict) -> FunctionModel:
    def handler(_messages, _info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[ToolCallPart("final_result", args)])

    return FunctionModel(handler)


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    from fwbg_agents.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")

    db_url = f"sqlite+aiosqlite:///{tmp_path}/critic.db"
    engine = create_async_engine(db_url, future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    yield Session
    await engine.dispose()


async def test_judge_requires_at_least_one_hypothesis(db):
    async with db() as session:
        with pytest.raises(ValueError, match="at least one"):
            await Critic(session, model=_stub_model({})).judge([])


async def test_judge_returns_report_and_agent_run_id(db):
    hyps = [_hyp("candidate A"), _hyp("candidate B")]
    args = {
        "candidates": [
            {"score": 0.8, "kill_risks": ["regime dependent"], "verdict": "pass"},
            {"score": 0.3, "kill_risks": ["no mechanism"], "verdict": "reject"},
        ],
        "winner_index": 0,
    }
    async with db() as session:
        report, ar_id = await Critic(session, model=_stub_model(args)).judge(hyps)

    assert isinstance(report, CriticReport)
    assert report.winner_index == 0
    assert report.candidates[0].verdict == "pass"
    assert report.candidates[1].verdict == "reject"

    async with db() as v:
        ar = (await v.execute(select(AgentRun).where(AgentRun.id == ar_id))).scalar_one()
        assert ar.status == AgentRunStatus.DONE.value
        assert ar.agent_name == "critic"
        calls = (
            (await v.execute(select(LlmCall).where(LlmCall.agent_run_id == ar_id))).scalars().all()
        )
        assert len(calls) == 1


async def test_judge_all_reject_sets_null_winner(db):
    hyps = [_hyp("candidate A"), _hyp("candidate B")]
    args = {
        "candidates": [
            {"score": 0.2, "kill_risks": ["no mechanism"], "verdict": "reject"},
            {"score": 0.1, "kill_risks": ["duplicate of abandoned idea"], "verdict": "reject"},
        ],
        "winner_index": None,
    }
    async with db() as session:
        report, _ar_id = await Critic(session, model=_stub_model(args)).judge(hyps)

    assert report.winner_index is None
    assert all(c.verdict == "reject" for c in report.candidates)


async def test_judge_mismatched_candidate_count_fails_run(db):
    hyps = [_hyp("candidate A"), _hyp("candidate B")]
    args = {
        "candidates": [{"score": 0.8, "kill_risks": [], "verdict": "pass"}],  # only 1, expected 2
        "winner_index": 0,
    }
    async with db() as session:
        with pytest.raises(ValueError, match="verdicts"):
            await Critic(session, model=_stub_model(args)).judge(hyps)

    async with db() as v:
        ar = (await v.execute(select(AgentRun))).scalars().all()[0]
        assert ar.status == AgentRunStatus.FAILED.value
