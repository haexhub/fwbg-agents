"""Tests for GET /economics/summary (Plan 018 — USD cost telemetry rollup)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from fwbg_agents.main import app
from fwbg_agents.persistence.database import Base, get_session
from fwbg_agents.persistence.models import AgentRun, LlmCall, Strategy, StrategyState


@pytest_asyncio.fixture
async def client_with_db(tmp_path, monkeypatch):
    from fwbg_agents.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")

    db_url = f"sqlite+aiosqlite:///{tmp_path}/api_test.db"
    engine = create_async_engine(db_url, future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    SessionMaker = async_sessionmaker(engine, expire_on_commit=False)
    session = SessionMaker()

    async def _override_get_session():
        yield session

    app.dependency_overrides[get_session] = _override_get_session

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        yield client, session

    app.dependency_overrides.clear()
    await session.close()
    await engine.dispose()


async def test_summary_empty_db(client_with_db):
    client, _session = client_with_db
    resp = await client.get("/economics/summary")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_input_tokens"] == 0
    assert body["total_output_tokens"] == 0
    assert body["total_cost_usd"] == 0.0
    assert body["unpriced_calls"] == 0
    assert body["by_agent"] == {}
    assert body["by_outcome"] == {}
    assert body["cost_per_promoted_strategy"] is None
    assert body["lineage_top"] == []


async def test_summary_totals_buckets_and_lineages(client_with_db):
    client, session = client_with_db
    now = datetime.now(UTC)

    def strategy(slug: str, state: str, parent_id: int | None = None) -> Strategy:
        return Strategy(
            slug=slug,
            current_state=state,
            strategy_family="momentum",
            parent_strategy_id=parent_id,
            created_at=now,
            updated_at=now,
        )

    def run(agent: str, strategy_id: int | None) -> AgentRun:
        return AgentRun(agent_name=agent, strategy_id=strategy_id, started_at=now, created_at=now)

    def call(ar_id: int, model: str, in_tok: int, out_tok: int, cost: float | None) -> LlmCall:
        return LlmCall(
            agent_run_id=ar_id,
            model=model,
            input_tokens=in_tok,
            output_tokens=out_tok,
            cost_usd=cost,
            created_at=now,
        )

    s_promoted = strategy("winner__fx__001", StrategyState.PAPER_TRADING.value)
    s_abandoned = strategy("loser__fx__001", StrategyState.ABANDONED.value)
    session.add_all([s_promoted, s_abandoned])
    await session.flush()
    # Child of the promoted lineage, itself abandoned — exercises root resolution.
    s_child = strategy("winner__fx__001__i2", StrategyState.ABANDONED.value, s_promoted.id)
    session.add(s_child)
    await session.flush()

    r_researcher = run("researcher", s_promoted.id)
    r_analyst = run("analyst", s_abandoned.id)
    r_critic = run("critic", None)  # unattributed
    r_child = run("analyst", s_child.id)
    session.add_all([r_researcher, r_analyst, r_critic, r_child])
    await session.flush()

    session.add_all(
        [
            call(r_researcher.id, "claude-opus-4-7", 1_000_000, 0, 5.0),
            call(r_analyst.id, "claude-opus-4-7", 0, 1_000_000, 25.0),
            call(r_critic.id, "tavily-search", 0, 0, None),  # unpriced pseudo-call
            call(r_critic.id, "claude-opus-4-7", 1_000_000, 1_000_000, 30.0),
            call(r_child.id, "claude-opus-4-7", 0, 0, 2.0),
        ]
    )
    await session.commit()

    resp = await client.get("/economics/summary")
    assert resp.status_code == 200
    body = resp.json()

    assert body["total_input_tokens"] == 2_000_000
    assert body["total_output_tokens"] == 2_000_000
    assert body["total_cost_usd"] == pytest.approx(62.0)
    assert body["unpriced_calls"] == 1

    assert body["by_agent"]["researcher"] == {
        "input_tokens": 1_000_000,
        "output_tokens": 0,
        "cost_usd": 5.0,
    }
    assert body["by_agent"]["analyst"]["cost_usd"] == pytest.approx(27.0)
    assert body["by_agent"]["critic"]["cost_usd"] == pytest.approx(30.0)

    assert body["by_outcome"]["paper_trading"]["cost_usd"] == pytest.approx(5.0)
    assert body["by_outcome"]["abandoned"]["cost_usd"] == pytest.approx(27.0)
    assert body["by_outcome"]["unattributed"]["cost_usd"] == pytest.approx(30.0)

    # 1 promoted strategy (paper_trading) -> total cost / 1.
    assert body["cost_per_promoted_strategy"] == pytest.approx(62.0)

    # Lineages sorted by cost desc; child cost rolls up to the promoted root,
    # whose outcome is the most advanced state in the lineage.
    assert body["lineage_top"] == [
        {"root_slug": "loser__fx__001", "total_cost_usd": 25.0, "outcome": "abandoned"},
        {"root_slug": "winner__fx__001", "total_cost_usd": 7.0, "outcome": "paper_trading"},
    ]
