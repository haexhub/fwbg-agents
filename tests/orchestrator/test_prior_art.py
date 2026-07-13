"""Tag-based prior-art lookup for the Researcher (M4)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from fwbg_agents.orchestrator.prior_art import lookup_prior_art
from fwbg_agents.persistence.database import Base
from fwbg_agents.persistence.models import Strategy, StrategyState, StrategyTag


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as s:
        yield s
    await engine.dispose()


async def _seed(
    session,
    slug,
    family,
    asset_class,
    tags,
    state=StrategyState.PROPOSED,
    post_mortem_path=None,
):
    now = datetime.now(UTC)
    s = Strategy(
        slug=slug,
        current_state=state.value,
        asset_class=asset_class,
        strategy_family=family,
        post_mortem_path=post_mortem_path,
        created_at=now,
        updated_at=now,
    )
    session.add(s)
    await session.flush()
    for t in tags:
        session.add(StrategyTag(strategy_id=s.id, tag=t))
    await session.commit()
    await session.refresh(s)
    return s


@pytest.mark.asyncio
async def test_returns_only_strategies_for_same_asset_class(db):
    await _seed(db, "a", "ORB", "FOREX", ["intraday", "momentum"])
    await _seed(db, "b", "ORB", "INDEX", ["intraday", "momentum"])
    matches = await lookup_prior_art(db, "ORB", "FOREX", ["intraday", "momentum"])
    assert [m.slug for m in matches] == ["a"]


@pytest.mark.asyncio
async def test_jaccard_below_threshold_is_filtered(db):
    await _seed(db, "a", "RSI", "FOREX", ["mean_reversion", "rsi"])
    matches = await lookup_prior_art(db, "ORB", "FOREX", ["momentum", "intraday"])
    assert matches == []


@pytest.mark.asyncio
async def test_same_family_matches_even_without_tag_overlap(db):
    await _seed(db, "a", "ORB", "FOREX", ["breakout"])
    matches = await lookup_prior_art(db, "ORB", "FOREX", ["intraday"])
    assert [m.slug for m in matches] == ["a"]


@pytest.mark.asyncio
async def test_results_sorted_by_jaccard_desc(db):
    await _seed(db, "low", "ORB", "FOREX", ["intraday", "x"])
    await _seed(db, "high", "ORB", "FOREX", ["intraday", "momentum", "trend"])
    matches = await lookup_prior_art(db, "ORB", "FOREX", ["intraday", "momentum"])
    assert [m.slug for m in matches] == ["high", "low"]


@pytest.mark.asyncio
async def test_strategy_with_no_tags_only_matches_via_family(db):
    await _seed(db, "no_tags", "ORB", "FOREX", [])
    await _seed(db, "rsi_no_tags", "RSI", "FOREX", [])
    matches = await lookup_prior_art(db, "ORB", "FOREX", ["intraday"])
    assert [m.slug for m in matches] == ["no_tags"]


@pytest.mark.asyncio
async def test_returns_empty_when_no_strategies(db):
    matches = await lookup_prior_art(db, "ORB", "FOREX", ["intraday"])
    assert matches == []


@pytest.mark.asyncio
async def test_tags_overlap_field_is_correct(db):
    await _seed(db, "a", "ORB", "FOREX", ["intraday", "momentum", "trend"])
    matches = await lookup_prior_art(db, "ORB", "FOREX", ["intraday", "momentum"])
    assert sorted(matches[0].tags_overlap) == ["intraday", "momentum"]


@pytest.mark.asyncio
async def test_post_mortem_summary_loaded_when_path_exists(db, tmp_path):
    pm = tmp_path / "post_mortem.yaml"
    pm.write_text("strategy_family: ORB\nabandon_reason: no edge in any regime\n")
    await _seed(
        db,
        "abandoned_a",
        "ORB",
        "FOREX",
        ["intraday"],
        state=StrategyState.ABANDONED,
        post_mortem_path=str(pm),
    )
    matches = await lookup_prior_art(db, "ORB", "FOREX", ["intraday"])
    assert matches[0].post_mortem_path == str(pm)
    assert matches[0].post_mortem_summary is not None
    assert "no edge" in matches[0].post_mortem_summary


@pytest.mark.asyncio
async def test_post_mortem_summary_is_none_when_file_missing(db):
    await _seed(
        db,
        "a",
        "ORB",
        "FOREX",
        ["intraday"],
        post_mortem_path="/nonexistent/path/post_mortem.yaml",
    )
    matches = await lookup_prior_art(db, "ORB", "FOREX", ["intraday"])
    assert matches[0].post_mortem_path is not None
    assert matches[0].post_mortem_summary is None


@pytest.mark.asyncio
async def test_post_mortem_summary_truncated_to_240_chars(db, tmp_path):
    pm = tmp_path / "post_mortem.yaml"
    pm.write_text("x" * 1000)
    await _seed(
        db,
        "a",
        "ORB",
        "FOREX",
        ["intraday"],
        post_mortem_path=str(pm),
    )
    matches = await lookup_prior_art(db, "ORB", "FOREX", ["intraday"])
    assert matches[0].post_mortem_summary is not None
    assert len(matches[0].post_mortem_summary) <= 240


@pytest.mark.asyncio
async def test_cap_at_20_matches(db):
    for i in range(30):
        await _seed(db, f"s_{i:03d}", "ORB", "FOREX", ["intraday", "momentum"])
    matches = await lookup_prior_art(db, "ORB", "FOREX", ["intraday", "momentum"])
    assert len(matches) == 20


@pytest.mark.asyncio
async def test_agnostic_lookup_scans_all_asset_classes(db):
    """asset_class=None must find strategies in other classes (not just None-class)."""
    await _seed(db, "forex_a", "ORB", "FOREX", ["intraday", "momentum"])
    await _seed(db, "index_b", "ORB", "INDEX", ["intraday", "momentum"])
    await _seed(db, "agnostic_c", "ORB", None, ["intraday", "momentum"])
    matches = await lookup_prior_art(db, "ORB", None, ["intraday", "momentum"])
    slugs = {m.slug for m in matches}
    assert slugs == {"forex_a", "index_b", "agnostic_c"}


@pytest.mark.asyncio
async def test_empty_string_asset_class_normalised_to_none(db):
    """LLM passes '' for asset-agnostic; must behave identically to None."""
    await _seed(db, "agnostic_a", "ORB", None, ["intraday"])
    await _seed(db, "forex_b", "ORB", "FOREX", ["intraday"])
    matches_none = await lookup_prior_art(db, "ORB", None, ["intraday"])
    matches_empty = await lookup_prior_art(db, "ORB", "", ["intraday"])
    assert {m.slug for m in matches_none} == {m.slug for m in matches_empty}


@pytest.mark.asyncio
async def test_match_surfaces_edge_mechanism_from_hypothesis(db, tmp_path, monkeypatch):
    """Plan 009 WP5: a match carries the hit's one-line edge anchor."""
    import json

    from fwbg_agents.config import settings
    from fwbg_agents.orchestrator.lifecycle import strategy_dir

    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    await _seed(db, "orb_a", "ORB", "FOREX", ["intraday", "momentum"])
    it_dir = strategy_dir("orb_a") / "iteration_001"
    it_dir.mkdir(parents=True, exist_ok=True)
    (it_dir / "hypothesis.json").write_text(
        json.dumps({"edge_mechanism": "London-open liquidity imbalance drives a momentum burst"})
    )
    matches = await lookup_prior_art(db, "ORB", "FOREX", ["intraday", "momentum"])
    assert matches[0].edge_mechanism == "London-open liquidity imbalance drives a momentum burst"
