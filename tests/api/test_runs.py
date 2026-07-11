"""Tests for the M3 runs endpoints.

POST /strategies/{id}/run     → kicks off the Runner in a background task
POST /strategies/{id}/analyze → kicks off the Analyst in a background task
GET  /agents/runs/{id}        → returns AgentRun status + paths

Tests patch the background-task entry points so we don't make real HTTP calls
to fwbg or talk to a real LLM. The endpoint code itself (input validation +
AgentRun bookkeeping) is what's under test here; Runner/Analyst behaviour is
covered by their own tests.
"""

from __future__ import annotations

import json
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
    Strategy,
    StrategyState,
)


@pytest_asyncio.fixture
async def runs_client(tmp_path, monkeypatch):
    from fwbg_agents.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")

    db_url = f"sqlite+aiosqlite:///{tmp_path}/runs.db"
    engine = create_async_engine(db_url, future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    session = Session()

    async def _override_get_session():
        yield session

    app.dependency_overrides[get_session] = _override_get_session

    # Seed strategies: one in PROPOSED for the run endpoint, one in BACKTESTED
    # with iteration_001 + fwbg_results.json for the analyze endpoint.
    now = datetime.now(UTC)
    proposed = Strategy(
        slug="prop_v1",
        current_state=StrategyState.PROPOSED.value,
        iteration_count=0,
        asset_class="FOREX",
        strategy_family="ORB",
        created_at=now,
        updated_at=now,
    )
    backtested = Strategy(
        slug="back_v1",
        current_state=StrategyState.BACKTESTED.value,
        iteration_count=0,
        asset_class="FOREX",
        strategy_family="ORB",
        created_at=now,
        updated_at=now,
    )
    session.add_all([proposed, backtested])
    await session.commit()
    await session.refresh(proposed)
    await session.refresh(backtested)

    # iteration dir + fwbg_results.json for backtested
    it = settings.data_dir / "strategies" / "back_v1" / "iteration_001"
    it.mkdir(parents=True, exist_ok=True)
    (it / "strategy.json").write_text(json.dumps({"name": "back_v1"}))
    (it / "fwbg_results.json").write_text(json.dumps({"status": "completed", "assets": {}}))

    # And iteration dir for the proposed one (Runner expects it)
    it2 = settings.data_dir / "strategies" / "prop_v1" / "iteration_001"
    it2.mkdir(parents=True, exist_ok=True)
    (it2 / "strategy.json").write_text(json.dumps({"name": "prop_v1"}))

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        yield client, session, proposed.id, backtested.id, tmp_path

    app.dependency_overrides.clear()
    await session.close()
    await engine.dispose()


async def test_post_run_creates_agent_run_and_returns_202(runs_client, monkeypatch):
    client, session, proposed_id, _, _ = runs_client

    captured = []

    async def fake_run_runner(strategy_id: int):
        captured.append(("runner", strategy_id))

    from fwbg_agents.api import runs as runs_api

    monkeypatch.setattr(runs_api, "_run_runner_background", fake_run_runner)

    r = await client.post(f"/strategies/{proposed_id}/run")
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["strategy_id"] == proposed_id
    assert "agent_run_id" in body

    # AgentRun row exists in PENDING/RUNNING
    ar = (
        await session.execute(select(AgentRun).where(AgentRun.id == body["agent_run_id"]))
    ).scalar_one()
    assert ar.agent_name == "runner"
    assert ar.strategy_id == proposed_id

    assert captured == [("runner", proposed_id)]


async def test_post_run_unknown_strategy_404(runs_client):
    client, _, _, _, _ = runs_client
    r = await client.post("/strategies/99999/run")
    assert r.status_code == 404


async def test_post_analyze_returns_202_when_results_present(runs_client, monkeypatch):
    client, _session, _, backtested_id, _ = runs_client
    captured = []

    async def fake_run_analyst(strategy_id: int):
        captured.append(("analyst", strategy_id))

    from fwbg_agents.api import runs as runs_api

    monkeypatch.setattr(runs_api, "_run_analyst_background", fake_run_analyst)

    r = await client.post(f"/strategies/{backtested_id}/analyze")
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["strategy_id"] == backtested_id
    assert captured == [("analyst", backtested_id)]


async def test_post_analyze_without_results_returns_409(runs_client):
    client, _, proposed_id, _, _ = runs_client
    r = await client.post(f"/strategies/{proposed_id}/analyze")
    assert r.status_code == 409
    assert "results" in r.json().get("detail", "").lower()


async def test_get_agent_run_returns_status(runs_client):
    client, session, _, _, _ = runs_client
    now = datetime.now(UTC)
    ar = AgentRun(
        agent_name="runner",
        status=AgentRunStatus.DONE.value,
        strategy_id=1,
        input_artifact_path="/in.json",
        output_artifact_path="/out.json",
        started_at=now,
        ended_at=now,
        created_at=now,
    )
    session.add(ar)
    await session.commit()
    await session.refresh(ar)

    r = await client.get(f"/agents/runs/{ar.id}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == ar.id
    assert body["status"] == "done"
    assert body["agent_name"] == "runner"
    assert body["output_artifact_path"] == "/out.json"


async def test_get_agent_run_exposes_parent_and_children(runs_client):
    """Flow drill-down: the detail endpoint returns parent_run_id and a list of
    child runs (Plan 008 Schritt 5)."""
    client, session, _, _, _ = runs_client
    now = datetime.now(UTC)
    flow = AgentRun(
        agent_name="research_flow",
        status=AgentRunStatus.DONE.value,
        started_at=now,
        ended_at=now,
        created_at=now,
    )
    session.add(flow)
    await session.commit()
    await session.refresh(flow)

    children = [
        AgentRun(
            agent_name=name,
            status=AgentRunStatus.DONE.value,
            parent_run_id=flow.id,
            started_at=now,
            ended_at=now,
            created_at=now,
        )
        for name in ("researcher", "translator")
    ]
    session.add_all(children)
    await session.commit()
    for c in children:
        await session.refresh(c)

    # Parent run: no parent, both children listed.
    r = await client.get(f"/agents/runs/{flow.id}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["parent_run_id"] is None
    assert [c["agent_name"] for c in body["children"]] == ["researcher", "translator"]
    assert all(c["status"] == "done" for c in body["children"])

    # Child run: points back at the flow, has no children of its own.
    r = await client.get(f"/agents/runs/{children[0].id}")
    assert r.status_code == 200, r.text
    child_body = r.json()
    assert child_body["parent_run_id"] == flow.id
    assert child_body["children"] == []


async def test_get_agent_run_404(runs_client):
    client, _, _, _, _ = runs_client
    r = await client.get("/agents/runs/99999")
    assert r.status_code == 404


async def test_get_agent_run_events_returns_emitted_events(runs_client):
    from fwbg_agents import run_events

    run_events._seq_cache.clear()
    client, session, _, _, _ = runs_client
    now = datetime.now(UTC)
    ar = AgentRun(
        agent_name="researcher",
        status=AgentRunStatus.RUNNING.value,
        started_at=now,
        created_at=now,
    )
    session.add(ar)
    await session.commit()
    await session.refresh(ar)

    run_events.emit_run_event(ar.id, "research_search", query="orb")
    run_events.emit_run_event(ar.id, "research_results", urls=[{"url": "u", "title": "t"}])

    r = await client.get(f"/agents/runs/{ar.id}/events")
    assert r.status_code == 200, r.text
    events = r.json()
    assert [e["type"] for e in events] == ["research_search", "research_results"]
    assert [e["seq"] for e in events] == [0, 1]
    assert events[0]["query"] == "orb"


async def test_get_agent_run_events_unknown_run_404(runs_client):
    client, _, _, _, _ = runs_client
    r = await client.get("/agents/runs/99999/events")
    assert r.status_code == 404


async def test_get_agent_run_events_empty_for_run_without_file(runs_client):
    from fwbg_agents import run_events

    run_events._seq_cache.clear()
    client, session, _, _, _ = runs_client
    now = datetime.now(UTC)
    ar = AgentRun(
        agent_name="runner",
        status=AgentRunStatus.DONE.value,
        started_at=now,
        ended_at=now,
        created_at=now,
    )
    session.add(ar)
    await session.commit()
    await session.refresh(ar)

    r = await client.get(f"/agents/runs/{ar.id}/events")
    assert r.status_code == 200, r.text
    assert r.json() == []


async def test_get_agent_run_detail_enriched(runs_client):
    from fwbg_agents.config import settings
    from fwbg_agents.persistence.models import LlmCall

    client, session, _, _, _ = runs_client
    now = datetime.now(UTC)
    ar = AgentRun(
        agent_name="researcher",
        status=AgentRunStatus.DONE.value,
        started_at=now,
        ended_at=now,
        created_at=now,
    )
    session.add(ar)
    await session.commit()
    await session.refresh(ar)
    session.add(
        LlmCall(
            agent_run_id=ar.id,
            model="claude-opus-4-8",
            input_tokens=100,
            output_tokens=40,
            latency_ms=1200,
            created_at=now,
        )
    )
    await session.commit()

    # A transcript file on disk for the run.
    rdir = settings.data_dir / "agent-runs" / str(ar.id)
    rdir.mkdir(parents=True, exist_ok=True)
    (rdir / "transcript_001.json").write_text("[]")

    r = await client.get(f"/agents/runs/{ar.id}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total_input_tokens"] == 100
    assert body["total_output_tokens"] == 40
    assert body["llm_calls"][0]["model"] == "claude-opus-4-8"
    assert body["transcripts"] == [{"round": 1, "size": 2}]
    assert {a["kind"] for a in body["artifacts"]} == {"input", "output"}


async def test_get_transcript_returns_json(runs_client):
    from fwbg_agents.config import settings

    client, session, _, _, _ = runs_client
    now = datetime.now(UTC)
    ar = AgentRun(
        agent_name="researcher",
        status=AgentRunStatus.DONE.value,
        started_at=now,
        ended_at=now,
        created_at=now,
    )
    session.add(ar)
    await session.commit()
    await session.refresh(ar)

    rdir = settings.data_dir / "agent-runs" / str(ar.id)
    rdir.mkdir(parents=True, exist_ok=True)
    (rdir / "transcript_002.json").write_text('[{"role": "system"}]')

    r = await client.get(f"/agents/runs/{ar.id}/transcript?round=2")
    assert r.status_code == 200, r.text
    assert r.json() == [{"role": "system"}]

    r404 = await client.get(f"/agents/runs/{ar.id}/transcript?round=9")
    assert r404.status_code == 404


async def test_get_artifact_happy_and_traversal_guard(runs_client):
    from fwbg_agents.config import settings

    client, session, _, _, _ = runs_client
    now = datetime.now(UTC)

    # A legitimate output artifact under data_dir.
    art = settings.data_dir / "strategies" / "s1" / "out.json"
    art.parent.mkdir(parents=True, exist_ok=True)
    art.write_text('{"ok": true}')

    ar = AgentRun(
        agent_name="researcher",
        status=AgentRunStatus.DONE.value,
        output_artifact_path=str(art),
        input_artifact_path="/etc/passwd",  # traversal attempt
        started_at=now,
        ended_at=now,
        created_at=now,
    )
    session.add(ar)
    await session.commit()
    await session.refresh(ar)

    ok = await client.get(f"/agents/runs/{ar.id}/artifact?kind=output")
    assert ok.status_code == 200, ok.text
    assert ok.json()["content"] == '{"ok": true}'

    blocked = await client.get(f"/agents/runs/{ar.id}/artifact?kind=input")
    assert blocked.status_code == 403


async def test_detail_artifact_metadata_hides_out_of_tree_paths(runs_client):
    """The detail endpoint must not leak existence/size of out-of-tree paths."""
    client, session, _, _, _ = runs_client
    now = datetime.now(UTC)
    ar = AgentRun(
        agent_name="researcher",
        status=AgentRunStatus.DONE.value,
        input_artifact_path="/etc/passwd",  # exists on disk, but outside data_dir
        started_at=now,
        ended_at=now,
        created_at=now,
    )
    session.add(ar)
    await session.commit()
    await session.refresh(ar)

    r = await client.get(f"/agents/runs/{ar.id}")
    assert r.status_code == 200, r.text
    inp = next(a for a in r.json()["artifacts"] if a["kind"] == "input")
    assert inp["exists"] is False
    assert inp["size"] is None
