"""Tests for GET /strategies/{id}/hypothesis and the universe/model-knowledge
fields on the strategy serializer.

The hypothesis JSON — including first-class `sources` with `key_points` and
the `suggested_universe` rationale — lives on disk at `hypothesis_path`, not in
the DB. The endpoint reads it back so the dashboard can surface an edge's
provenance. `suggested_universe` / `model_knowledge_only` are columns and are
exposed directly by the list/detail serializer.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from fwbg_agents.main import app
from fwbg_agents.persistence.database import Base, get_session
from fwbg_agents.persistence.models import Strategy, StrategyState


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
        yield client, session, tmp_path

    app.dependency_overrides.clear()
    await session.close()
    await engine.dispose()


_HYPOTHESIS = {
    "title": "Overnight drift in equity index futures",
    "asset_class": "INDEX",
    "strategy_family": "momentum",
    "hypothesis": "Index futures drift up overnight due to risk-transfer premia.",
    "expected_edge_explanation": "Dealers hedge into the close; demand persists.",
    "key_indicators": ["overnight_return", "vix"],
    "tags": ["overnight", "index"],
    "sources": [
        {
            "url": "https://example.com/paper",
            "title": "Overnight Returns and Firm-Specific Investor Sentiment",
            "why_relevant": "Documents the overnight drift anomaly.",
            "key_points": [
                "Overnight returns are systematically positive.",
                "Effect concentrates in high-sentiment names.",
            ],
        }
    ],
    "suggested_universe": [
        {
            "scope": "asset_class",
            "value": "INDEX",
            "timeframe": "HOUR_1",
            "rationale": "Broadest expression of the overnight-drift edge.",
        }
    ],
    "model_knowledge_only": False,
    "differentiates_from": [],
}


async def _make_strategy(
    session,
    *,
    slug: str,
    hypothesis_path: str | None = None,
    suggested_universe: list | None = None,
    model_knowledge_only: bool = False,
) -> Strategy:
    now = datetime.now(UTC)
    s = Strategy(
        slug=slug,
        current_state=StrategyState.PROPOSED.value,
        iteration_count=1,
        asset_class="INDEX",
        strategy_family="momentum",
        hypothesis_path=hypothesis_path,
        suggested_universe=suggested_universe,
        model_knowledge_only=model_knowledge_only,
        created_at=now,
        updated_at=now,
    )
    session.add(s)
    await session.commit()
    await session.refresh(s)
    return s


def _write_hypothesis(tmp_path, slug: str, payload: dict) -> str:
    iteration_dir = tmp_path / "data" / "strategies" / slug / "iteration_001"
    iteration_dir.mkdir(parents=True, exist_ok=True)
    path = iteration_dir / "hypothesis.json"
    path.write_text(json.dumps(payload, indent=2))
    return str(path)


# ---------------------------------------------------------------------------
# Serializer fields
# ---------------------------------------------------------------------------


async def test_detail_exposes_universe_and_model_knowledge(client_with_db):
    client, session, _tmp = client_with_db
    s = await _make_strategy(
        session,
        slug="momentum__index__001",
        suggested_universe=[{"scope": "asset_class", "value": "INDEX", "rationale": "x"}],
        model_knowledge_only=True,
    )
    r = await client.get(f"/strategies/{s.id}")
    assert r.status_code == 200, r.text
    strat = r.json()["strategy"]
    assert strat["model_knowledge_only"] is True
    assert strat["suggested_universe"] == [
        {"scope": "asset_class", "value": "INDEX", "rationale": "x"}
    ]


async def test_list_exposes_universe_and_model_knowledge(client_with_db):
    client, session, _tmp = client_with_db
    await _make_strategy(session, slug="momentum__index__001", model_knowledge_only=False)
    r = await client.get("/strategies")
    assert r.status_code == 200, r.text
    row = r.json()["strategies"][0]
    assert "suggested_universe" in row
    assert row["model_knowledge_only"] is False


# ---------------------------------------------------------------------------
# GET /strategies/{id}/hypothesis
# ---------------------------------------------------------------------------


async def test_hypothesis_returns_sources_and_universe(client_with_db):
    client, session, tmp_path = client_with_db
    path = _write_hypothesis(tmp_path, "momentum__index__001", _HYPOTHESIS)
    s = await _make_strategy(session, slug="momentum__index__001", hypothesis_path=path)

    r = await client.get(f"/strategies/{s.id}/hypothesis")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["strategy_id"] == s.id
    assert body["slug"] == "momentum__index__001"
    hyp = body["hypothesis"]
    assert hyp["title"] == _HYPOTHESIS["title"]
    assert len(hyp["sources"]) == 1
    assert hyp["sources"][0]["key_points"] == [
        "Overnight returns are systematically positive.",
        "Effect concentrates in high-sentiment names.",
    ]
    assert hyp["suggested_universe"][0]["scope"] == "asset_class"


async def test_hypothesis_404_for_unknown_strategy(client_with_db):
    client, _session, _tmp = client_with_db
    r = await client.get("/strategies/9999/hypothesis")
    assert r.status_code == 404


async def test_hypothesis_404_when_no_hypothesis_path(client_with_db):
    client, session, _tmp = client_with_db
    s = await _make_strategy(session, slug="momentum__index__001", hypothesis_path=None)
    r = await client.get(f"/strategies/{s.id}/hypothesis")
    assert r.status_code == 404
    assert "no hypothesis" in r.json()["detail"].lower()


async def test_hypothesis_404_when_file_missing(client_with_db):
    client, session, tmp_path = client_with_db
    ghost = str(tmp_path / "data" / "strategies" / "gone" / "iteration_001" / "hypothesis.json")
    s = await _make_strategy(session, slug="momentum__index__001", hypothesis_path=ghost)
    r = await client.get(f"/strategies/{s.id}/hypothesis")
    assert r.status_code == 404
    assert "missing" in r.json()["detail"].lower()


async def test_hypothesis_404_when_path_outside_data_dir(client_with_db):
    client, session, _tmp = client_with_db
    s = await _make_strategy(session, slug="momentum__index__001", hypothesis_path="/etc/passwd")
    r = await client.get(f"/strategies/{s.id}/hypothesis")
    assert r.status_code == 404
    assert "outside" in r.json()["detail"].lower()
