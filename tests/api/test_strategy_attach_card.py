"""PUT /strategies/{id}/research — attach an existing register card to a lineage root."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import ClassVar

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from fwbg_agents.api import strategies as strategies_api
from fwbg_agents.main import app
from fwbg_agents.persistence.database import Base, get_session
from fwbg_agents.persistence.models import Strategy, StrategyState
from fwbg_agents.tools.fwbg_client import FwbgClientError


class _Fwbg:
    """Stands in for FwbgClient; ``error`` is raised by get_research_card."""

    error: ClassVar[Exception | None] = None
    asked: ClassVar[list[str]] = []
    closed: ClassVar[int] = 0

    def __init__(self, **_kwargs):
        pass

    async def get_research_card(self, research_id):
        type(self).asked.append(research_id)
        if self.error:
            raise self.error
        return {"card": {"id": research_id}}

    async def aclose(self):
        type(self).closed += 1


@pytest_asyncio.fixture
async def api(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/attach.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session = async_sessionmaker(engine, expire_on_commit=False)()

    async def _override_get_session():
        yield session

    app.dependency_overrides[get_session] = _override_get_session
    _Fwbg.error, _Fwbg.asked, _Fwbg.closed = None, [], 0
    monkeypatch.setattr(strategies_api, "FwbgClient", _Fwbg)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        yield client, session
    app.dependency_overrides.clear()
    await session.close()
    await engine.dispose()


async def _seed(session, slug, *, parent_id=None, metadata=None) -> Strategy:
    now = datetime.now(UTC)
    row = Strategy(
        slug=slug,
        current_state=StrategyState.PROPOSED.value,
        iteration_count=1,
        parent_strategy_id=parent_id,
        asset_class="FOREX",
        strategy_family="ORB",
        metadata_json=metadata or {},
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


async def test_attach_stores_the_card_on_the_root_and_shows_it(api):
    client, session = api
    root = await _seed(session, "root")

    r = await client.put(f"/strategies/{root.id}/research", json={"research_id": "S-0007"})

    assert r.status_code == 200
    assert r.json() == {"strategy_id": root.id, "research_id": "S-0007"}
    assert _Fwbg.asked == ["S-0007"] and _Fwbg.closed == 1
    shown = (await client.get(f"/strategies/{root.id}")).json()["strategy"]
    assert shown["research_id"] == "S-0007"


async def test_attach_keeps_other_metadata(api):
    client, session = api
    root = await _seed(session, "root", metadata={"asset_specific": True})

    await client.put(f"/strategies/{root.id}/research", json={"research_id": "S-0007"})

    await session.refresh(root)
    assert root.metadata_json == {"asset_specific": True, "research_id": "S-0007"}


async def test_attach_same_card_again_is_a_noop_without_asking_fwbg(api):
    client, session = api
    root = await _seed(session, "root", metadata={"research_id": "S-0007"})

    r = await client.put(f"/strategies/{root.id}/research", json={"research_id": "S-0007"})

    assert r.status_code == 200 and _Fwbg.asked == []


@pytest.mark.parametrize("body", [{"research_id": "S-12"}, {"research_id": "x"}, {}])
async def test_attach_rejects_a_malformed_id(api, body):
    client, session = api
    root = await _seed(session, "root")
    assert (await client.put(f"/strategies/{root.id}/research", json=body)).status_code == 422


async def test_attach_refuses_a_second_card(api):
    client, session = api
    root = await _seed(session, "root", metadata={"research_id": "S-0007"})
    r = await client.put(f"/strategies/{root.id}/research", json={"research_id": "S-0008"})
    assert r.status_code == 409 and "S-0007" in r.json()["detail"]


async def test_attach_refuses_a_child(api):
    client, session = api
    root = await _seed(session, "root")
    child = await _seed(session, "child", parent_id=root.id)
    r = await client.put(f"/strategies/{child.id}/research", json={"research_id": "S-0007"})
    assert r.status_code == 409


async def test_attach_unknown_strategy_is_404(api):
    client, _session = api
    r = await client.put("/strategies/999/research", json={"research_id": "S-0007"})
    assert r.status_code == 404


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (FwbgClientError(404, "no such card"), 404),
        (FwbgClientError(500, "boom"), 502),
        (httpx.ConnectError("down"), 502),
    ],
)
async def test_attach_maps_register_errors_and_stores_nothing(api, error, status):
    client, session = api
    root = await _seed(session, "root")
    _Fwbg.error = error

    r = await client.put(f"/strategies/{root.id}/research", json={"research_id": "S-0007"})

    assert r.status_code == status
    await session.refresh(root)
    assert "research_id" not in root.metadata_json
    assert _Fwbg.closed == 1
