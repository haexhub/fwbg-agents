"""Interventions-digest tests (Plan 010 WP5)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from fwbg_agents.orchestrator.interventions import (
    interventions_digest,
    regenerate_interventions_digest,
)
from fwbg_agents.persistence.database import Base
from fwbg_agents.persistence.models import Strategy, StrategyState, Transition


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    from fwbg_agents.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")

    db_url = f"sqlite+aiosqlite:///{tmp_path}/interventions.db"
    engine = create_async_engine(db_url, future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    yield Session, settings
    await engine.dispose()


def _write_results(settings, slug: str, sharpes: list[float]) -> None:
    it_dir = settings.data_dir / "strategies" / slug / "iteration_001"
    it_dir.mkdir(parents=True, exist_ok=True)
    (it_dir / "fwbg_results.json").write_text(
        json.dumps(
            {
                "assets": {
                    f"SYM{i}": {"unified_metrics": {"sharpe": sh}} for i, sh in enumerate(sharpes)
                }
            }
        )
    )


async def _seed_edge(
    Session, settings, *, parent_slug, parent_sharpes, child_slug, child_sharpes, family, kind
):
    async with Session() as session:
        now = datetime.now(UTC)
        parent = Strategy(
            slug=parent_slug,
            current_state=StrategyState.BACKTESTED.value,
            iteration_count=1,
            asset_class="FOREX",
            strategy_family=family,
            created_at=now,
            updated_at=now,
        )
        session.add(parent)
        await session.flush()

        child = Strategy(
            slug=child_slug,
            current_state=StrategyState.BACKTESTED.value,
            iteration_count=2,
            parent_strategy_id=parent.id,
            asset_class="FOREX",
            strategy_family=family,
            created_at=now,
            updated_at=now,
        )
        session.add(child)
        await session.flush()

        session.add(
            Transition(
                entity_type="strategy",
                entity_id=child.id,
                from_state=None,
                to_state=StrategyState.PROPOSED.value,
                reason="translator: reiterate",
                payload={"recommendation": {"kind": kind}},
                created_by="translator",
                created_at=now,
            )
        )
        await session.commit()

    _write_results(settings, parent_slug, parent_sharpes)
    _write_results(settings, child_slug, child_sharpes)


async def test_empty_db_yields_placeholder(db):
    Session, _settings = db
    async with Session() as session:
        await regenerate_interventions_digest(session)
    digest = interventions_digest()
    assert "no completed interventions" in digest


async def test_no_digest_file_yet_yields_placeholder(db):
    """interventions_digest() before any regenerate call must not crash."""
    digest = interventions_digest()
    assert "no interventions digest yet" in digest


async def test_aggregates_median_delta_by_family_and_kind(db):
    Session, settings = db
    await _seed_edge(
        Session,
        settings,
        parent_slug="mr_a",
        parent_sharpes=[1.0],
        child_slug="mr_a__it002",
        child_sharpes=[1.3],
        family="mean_reversion",
        kind="change_exit",
    )
    await _seed_edge(
        Session,
        settings,
        parent_slug="mr_b",
        parent_sharpes=[0.8],
        child_slug="mr_b__it002",
        child_sharpes=[1.1],
        family="mean_reversion",
        kind="change_exit",
    )
    await _seed_edge(
        Session,
        settings,
        parent_slug="orb_a",
        parent_sharpes=[0.5],
        child_slug="orb_a__it002",
        child_sharpes=[0.4],
        family="ORB",
        kind="tune_params",
    )

    async with Session() as session:
        await regenerate_interventions_digest(session)
    digest = interventions_digest()

    # mean_reversion x change_exit: deltas [0.3, 0.3] -> median 0.3, n=2
    assert "mean_reversion x change_exit: median Δsharpe=+0.30 (n=2)" in digest
    # ORB x tune_params: delta [-0.1] -> median -0.1, n=1
    assert "ORB x tune_params: median Δsharpe=-0.10 (n=1)" in digest


async def test_reiterate_with_plugin_edge_counts_as_add_indicator(db):
    """run_reiterate_with_plugin transitions have no `recommendation` dict in
    their payload — the plugin_slug marker maps them to the add_indicator
    lever, so the digest covers all four levers it advertises."""
    Session, settings = db
    async with Session() as session:
        now = datetime.now(UTC)
        parent = Strategy(
            slug="mr_p",
            current_state=StrategyState.BACKTESTED.value,
            iteration_count=1,
            asset_class="FOREX",
            strategy_family="mean_reversion",
            created_at=now,
            updated_at=now,
        )
        session.add(parent)
        await session.flush()
        child = Strategy(
            slug="mr_p__plugin",
            current_state=StrategyState.BACKTESTED.value,
            iteration_count=1,
            parent_strategy_id=parent.id,
            asset_class="FOREX",
            strategy_family="mean_reversion",
            created_at=now,
            updated_at=now,
        )
        session.add(child)
        await session.flush()
        session.add(
            Transition(
                entity_type="strategy",
                entity_id=child.id,
                from_state=None,
                to_state=StrategyState.PROPOSED.value,
                reason="translator: reiterate_with_plugin",
                payload={
                    "parent_strategy_id": parent.id,
                    "plugin_slug": "atr-bands",
                    "sidecar": {},
                },
                created_by="translator",
                created_at=now,
            )
        )
        await session.commit()

    _write_results(settings, "mr_p", [1.0])
    _write_results(settings, "mr_p__plugin", [1.4])

    async with Session() as session:
        await regenerate_interventions_digest(session)
    digest = interventions_digest()
    assert "mean_reversion x add_indicator: median Δsharpe=+0.40 (n=1)" in digest


async def test_edge_without_recognized_recommendation_kind_is_excluded(db):
    Session, settings = db
    async with Session() as session:
        now = datetime.now(UTC)
        parent = Strategy(
            slug="p",
            current_state=StrategyState.BACKTESTED.value,
            iteration_count=1,
            asset_class="FOREX",
            strategy_family="ORB",
            created_at=now,
            updated_at=now,
        )
        session.add(parent)
        await session.flush()
        child = Strategy(
            slug="c",
            current_state=StrategyState.BACKTESTED.value,
            iteration_count=2,
            parent_strategy_id=parent.id,
            asset_class="FOREX",
            strategy_family="ORB",
            created_at=now,
            updated_at=now,
        )
        session.add(child)
        await session.commit()
        # No creation Transition at all for the child.

    _write_results(settings, "p", [1.0])
    _write_results(settings, "c", [1.5])

    async with Session() as session:
        await regenerate_interventions_digest(session)
    digest = interventions_digest()
    assert "no completed interventions" in digest


async def test_max_chars_caps_digest(db):
    Session, settings = db
    for i in range(30):
        await _seed_edge(
            Session,
            settings,
            parent_slug=f"p{i}",
            parent_sharpes=[1.0],
            child_slug=f"c{i}",
            child_sharpes=[1.2],
            family=f"family_{i}",
            kind="tune_params",
        )
    async with Session() as session:
        await regenerate_interventions_digest(session)
    digest = interventions_digest(max_chars=100)
    assert len(digest) <= 100
