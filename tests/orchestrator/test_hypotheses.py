"""Researcher hypothesis schema, validator, and deterministic slug generation (M4)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import pytest_asyncio
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from fwbg_agents.orchestrator.hypotheses import (
    HypothesisRejected,
    ResearcherHypothesis,
    Source,
    generate_slug,
    validate_hypothesis,
)
from fwbg_agents.orchestrator.prior_art import PriorArtMatch
from fwbg_agents.persistence.database import Base
from fwbg_agents.persistence.models import Strategy, StrategyState


def _hyp(**over):
    base = dict(
        title="t",
        asset_class="FOREX",
        strategy_family="ORB",
        hypothesis="h",
        expected_edge_explanation="e",
        key_indicators=["opening_range"],
        tags=["momentum"],
        sources=[Source(url="https://x", title="x", why_relevant="x")],
        differentiates_from=[],
    )
    base.update(over)
    return ResearcherHypothesis(**base)


def _match(slug="prev_orb_001"):
    return PriorArtMatch(
        slug=slug,
        current_state="abandoned",
        strategy_family="ORB",
        asset_class="FOREX",
        tags_overlap=["momentum"],
        jaccard=0.5,
        post_mortem_path=None,
        post_mortem_summary=None,
    )


# --- validate_hypothesis ---


def test_validate_passes_with_no_prior_art():
    validate_hypothesis(_hyp(), [])


def test_validate_rejects_when_prior_art_and_no_differentiates_from():
    with pytest.raises(HypothesisRejected):
        validate_hypothesis(_hyp(), [_match()])


def test_validate_passes_when_differentiates_from_covers_prior_art():
    validate_hypothesis(_hyp(differentiates_from=["prev_orb_001"]), [_match()])


def test_validate_rejects_when_differentiates_from_slug_unknown():
    with pytest.raises(HypothesisRejected):
        validate_hypothesis(_hyp(differentiates_from=["unrelated"]), [_match()])


def test_validate_rejects_when_partial_differentiates_from():
    """All prior-art slugs must be addressed, not just some."""
    matches = [_match("prev_orb_001"), _match("prev_orb_002")]
    with pytest.raises(HypothesisRejected):
        validate_hypothesis(_hyp(differentiates_from=["prev_orb_001"]), matches)


# --- ResearcherHypothesis schema ---


def test_hypothesis_tags_min_length_enforced():
    with pytest.raises(ValidationError):
        _hyp(tags=[])


def test_hypothesis_sources_min_length_enforced():
    with pytest.raises(ValidationError):
        _hyp(sources=[])


def test_hypothesis_key_indicators_min_length_enforced():
    with pytest.raises(ValidationError):
        _hyp(key_indicators=[])


# --- generate_slug ---


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as s:
        yield s
    await engine.dispose()


async def _seed_strategy(session, slug, family, asset_class):
    now = datetime.now(UTC)
    s = Strategy(
        slug=slug,
        current_state=StrategyState.PROPOSED.value,
        asset_class=asset_class,
        strategy_family=family,
        created_at=now,
        updated_at=now,
    )
    session.add(s)
    await session.commit()


@pytest.mark.asyncio
async def test_generate_slug_starts_at_001(db):
    slug = await generate_slug(db, "ORB", "FOREX")
    assert slug == "orb__forex__001"


@pytest.mark.asyncio
async def test_generate_slug_increments_per_family_asset_pair(db):
    await _seed_strategy(db, "orb__forex__001", "ORB", "FOREX")
    assert await generate_slug(db, "ORB", "FOREX") == "orb__forex__002"
    assert await generate_slug(db, "RSI", "FOREX") == "rsi__forex__001"


@pytest.mark.asyncio
async def test_generate_slug_strips_special_chars(db):
    slug = await generate_slug(db, "RSI/EMA-Cross", "FOREX")
    assert slug == "rsiemacross__forex__001"


@pytest.mark.asyncio
async def test_generate_slug_finds_max_even_with_gaps(db):
    await _seed_strategy(db, "orb__forex__001", "ORB", "FOREX")
    await _seed_strategy(db, "orb__forex__007", "ORB", "FOREX")
    assert await generate_slug(db, "ORB", "FOREX") == "orb__forex__008"


@pytest.mark.asyncio
async def test_generate_slug_ignores_unrelated_slugs(db):
    # an unrelated slug shouldn't poison the counter
    await _seed_strategy(db, "some_manual_smoke_strategy", "ORB", "FOREX")
    assert await generate_slug(db, "ORB", "FOREX") == "orb__forex__001"
