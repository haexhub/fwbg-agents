"""POST /strategies/{id}/abandon — operator retirement via the state machine.

Abandoning (not deleting — rows are append-only) removes a strategy from the
auto-runner queue, writes an operator post-mortem, and logs a Transition.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from fwbg_agents.main import app
from fwbg_agents.persistence.database import Base, get_session
from fwbg_agents.persistence.models import Strategy, StrategyState, Transition


@pytest_asyncio.fixture
async def client_with_db(tmp_path, monkeypatch):
    from fwbg_agents.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/abandon.db")
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


async def _seed(session, slug: str, state: StrategyState) -> Strategy:
    now = datetime.now(UTC)
    row = Strategy(
        slug=slug, current_state=state.value, iteration_count=1,
        asset_class="FOREX", strategy_family="ORB",
        created_at=now, updated_at=now,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


async def test_abandon_proposed_strategy(client_with_db):
    from fwbg_agents.config import settings

    client, session = client_with_db
    s = await _seed(session, "orb__forex__001", StrategyState.PROPOSED)

    resp = await client.post(
        f"/strategies/{s.id}/abandon",
        json={"reason": "stale strategy.json from the frozen-catalog era"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["new_state"] == "abandoned"

    await session.refresh(s)
    assert s.current_state == StrategyState.ABANDONED.value
    post_mortem = settings.data_dir / "strategies" / s.slug / "post_mortem.md"
    assert post_mortem.is_file()
    assert "frozen-catalog era" in post_mortem.read_text()
    assert s.post_mortem_path == str(post_mortem)

    transitions = (
        (await session.execute(select(Transition))).scalars().all()
    )
    assert len(transitions) == 1
    assert transitions[0].to_state == "abandoned"
    assert transitions[0].created_by == "operator"


async def test_abandon_unknown_strategy_404(client_with_db):
    client, _ = client_with_db
    resp = await client.post("/strategies/999/abandon", json={"reason": "x"})
    assert resp.status_code == 404


async def test_abandon_already_abandoned_409(client_with_db):
    client, session = client_with_db
    s = await _seed(session, "orb__forex__002", StrategyState.ABANDONED)
    resp = await client.post(f"/strategies/{s.id}/abandon", json={"reason": "again"})
    assert resp.status_code == 409


async def test_abandon_requires_reason(client_with_db):
    client, session = client_with_db
    s = await _seed(session, "orb__forex__003", StrategyState.PROPOSED)
    resp = await client.post(f"/strategies/{s.id}/abandon", json={"reason": ""})
    assert resp.status_code == 422
