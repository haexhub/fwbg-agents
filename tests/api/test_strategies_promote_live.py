"""Tests for the M6b promote-live endpoint.

POST /strategies/{id}/promote-live   -> 200, transitions PAPER_TRADING → LIVE_TRADING

Triple-gated:
  1. body.human_approval is True               (422 otherwise)
  2. metadata_json.paper_analyst_promote_recommended is True   (422 otherwise)
  3. strategy.current_state == PAPER_TRADING   (422 otherwise)

The lifecycle guard (M2 _guard_strategy_paper_to_live) re-checks gate 1's
payload["human_approval"] — that gate cannot be bypassed by the analyst
because the analyst is never the HTTP caller here; only an operator is.

No LLM is invoked — this is a pure audit + state-transition endpoint.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from fwbg_agents.main import app
from fwbg_agents.persistence.database import Base, get_session
from fwbg_agents.persistence.models import (
    AgentRun,
    AgentRunStatus,
    EntityType,
    Strategy,
    StrategyState,
    Transition,
)


@pytest_asyncio.fixture
async def client_with_db(tmp_path, monkeypatch):
    from fwbg_agents.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path / "agents-data")
    fwbg_data_dir = tmp_path / "fwbg-data"
    fwbg_data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(settings, "fwbg_data_dir", fwbg_data_dir)

    db_url = f"sqlite+aiosqlite:///{tmp_path}/promote_live.db"
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
        yield client, session

    app.dependency_overrides.clear()
    await session.close()
    await engine.dispose()


async def _seed_strategy(
    session,
    *,
    slug: str,
    state: str,
    promote_recommended: bool = False,
) -> Strategy:
    now = datetime.now(UTC)
    metadata: dict = {}
    if promote_recommended:
        metadata["paper_analyst_promote_recommended"] = True
    s = Strategy(
        slug=slug,
        current_state=state,
        iteration_count=1,
        asset_class="forex",
        strategy_family="ORB",
        paper_phase_target_days=90,
        metadata_json=metadata,
        created_at=now,
        updated_at=now,
    )
    session.add(s)
    await session.commit()
    await session.refresh(s)
    return s


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_promote_live_returns_404_when_strategy_missing(client_with_db):
    client, _ = client_with_db
    r = await client.post(
        "/strategies/99999/promote-live",
        json={"human_approval": True},
    )
    assert r.status_code == 404, r.text
    assert "not found" in r.json()["detail"]


async def test_promote_live_returns_422_when_human_approval_false(client_with_db):
    client, session = client_with_db
    s = await _seed_strategy(
        session,
        slug="no_approval",
        state=StrategyState.PAPER_TRADING.value,
        promote_recommended=True,
    )
    r = await client.post(
        f"/strategies/{s.id}/promote-live",
        json={"human_approval": False},
    )
    assert r.status_code == 422, r.text
    assert "human_approval" in r.json()["detail"].lower()


async def test_promote_live_returns_422_when_strategy_not_in_paper_trading(
    client_with_db,
):
    client, session = client_with_db
    s = await _seed_strategy(
        session,
        slug="not_paper",
        state=StrategyState.PROPOSED.value,
        promote_recommended=True,
    )
    r = await client.post(
        f"/strategies/{s.id}/promote-live",
        json={"human_approval": True},
    )
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert "PAPER_TRADING" in detail
    assert "proposed" in detail or StrategyState.PROPOSED.value in detail


async def test_promote_live_returns_422_when_no_promote_recommendation_flag(
    client_with_db,
):
    client, session = client_with_db
    s = await _seed_strategy(
        session,
        slug="no_flag",
        state=StrategyState.PAPER_TRADING.value,
        promote_recommended=False,
    )
    r = await client.post(
        f"/strategies/{s.id}/promote-live",
        json={"human_approval": True},
    )
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert "paper_analyst_promote_recommended" in detail
    assert "paper-analyze" in detail


async def test_promote_live_happy_path_transitions_to_live_trading(client_with_db):
    client, session = client_with_db
    s = await _seed_strategy(
        session,
        slug="ready_to_live",
        state=StrategyState.PAPER_TRADING.value,
        promote_recommended=True,
    )

    r = await client.post(
        f"/strategies/{s.id}/promote-live",
        json={"human_approval": True, "operator_note": "looks good"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["strategy_id"] == s.id
    assert body["new_state"] == StrategyState.LIVE_TRADING.value
    assert "agent_run_id" in body

    await session.refresh(s)
    assert s.current_state == StrategyState.LIVE_TRADING.value

    ar = (
        await session.execute(
            select(AgentRun).where(AgentRun.id == body["agent_run_id"])
        )
    ).scalar_one()
    assert ar.agent_name == "promote_live"
    assert ar.status == AgentRunStatus.DONE.value
    assert ar.strategy_id == s.id
    assert ar.ended_at is not None


async def test_promote_live_records_audit_transition_with_human_approval_payload(
    client_with_db,
):
    client, session = client_with_db
    s = await _seed_strategy(
        session,
        slug="audit_check",
        state=StrategyState.PAPER_TRADING.value,
        promote_recommended=True,
    )

    r = await client.post(
        f"/strategies/{s.id}/promote-live",
        json={"human_approval": True, "operator_note": "approved by ops"},
    )
    assert r.status_code == 200, r.text

    rows = (
        await session.execute(
            select(Transition)
            .where(
                (Transition.entity_type == EntityType.STRATEGY.value)
                & (Transition.entity_id == s.id)
            )
            .order_by(Transition.id.desc())
        )
    ).scalars().all()
    assert rows, "no Transition row was written"
    t = rows[0]
    assert t.entity_type == EntityType.STRATEGY.value
    assert t.from_state == StrategyState.PAPER_TRADING.value
    assert t.to_state == StrategyState.LIVE_TRADING.value
    assert t.payload.get("human_approval") is True
    assert t.payload.get("operator_note") == "approved by ops"
    assert t.created_by == "operator"
