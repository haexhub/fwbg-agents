"""Researcher hypothesis schema, validator, and deterministic slug generator (M4).

The pydantic models live here (not in researcher.py) so the validator and
slug generator can be imported without pulling in pydantic-ai or LLM clients.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fwbg_agents.orchestrator.prior_art import PriorArtMatch
from fwbg_agents.persistence.models import Strategy
from fwbg_agents.speckit.strategy_spec import StrategyFamilyLit, StrategySpec

_SLUG_SUFFIX_RE = re.compile(r"__(\d{3,})$")
_ITER_SUFFIX_RE = re.compile(r"__it(\d{3,})$")


class HypothesisRejectedError(ValueError):
    """Raised by validate_hypothesis when the Researcher output conflicts with prior art."""


class Source(BaseModel):
    """A research source cited by the Researcher agent."""

    url: str
    title: str
    why_relevant: str
    key_points: list[str] = Field(default_factory=list)


class SuggestedUniverse(BaseModel):
    """One entry in the Researcher's asset recommendation.

    scope="asset_class" covers the whole class; scope="symbol" pins to one
    instrument. `value` is validated against fwbg's vocabulary at persist time.
    """

    scope: Literal["symbol", "asset_class"]
    value: str
    timeframe: str | None = None
    rationale: str


class ResearcherHypothesis(BaseModel):
    """Structured output of the Researcher agent.

    `differentiates_from` lists slugs of prior strategies this hypothesis
    deliberately deviates from. Required by `validate_hypothesis` whenever
    `lookup_prior_art` returned matches.

    `asset_class` is optional — None means asset-agnostic research. When set,
    it must match fwbg's controlled vocabulary (validated at API intake).

    `model_knowledge_only` must be True when no web-search was available and
    all sources come from training knowledge (url="n/a (model knowledge)").
    """

    title: str
    asset_class: str | None = None
    strategy_family: StrategyFamilyLit
    edge_mechanism: str = Field(
        min_length=10,
        max_length=240,
        description="ONE sentence: the mechanism that creates the edge. The "
        "dedup anchor — two strategies with the same edge_mechanism are the same idea.",
    )
    hypothesis: str
    expected_edge_explanation: str
    entry_logic: str = ""
    exit_mechanism: str = ""
    regime_assumption: str = ""
    filters: list[str] = Field(default_factory=list)
    key_indicators: list[str] = Field(min_length=1)
    tags: list[str] = Field(min_length=1)
    sources: list[Source] = Field(min_length=1)
    suggested_universe: list[SuggestedUniverse] = Field(default_factory=list)
    model_knowledge_only: bool = False
    differentiates_from: list[str] = Field(default_factory=list)
    asset_specific: bool = False
    asset_specific_rationale: str = ""


def strategy_spec_from_hypothesis(hypothesis: ResearcherHypothesis) -> StrategySpec:
    """Derive a StrategySpec from a validated hypothesis (Plan 009 WP5).

    Timeframe + universe come from `suggested_universe`; the differentiation
    dimensions come from the (optional) structured hypothesis fields.
    """
    timeframe = next((u.timeframe for u in hypothesis.suggested_universe if u.timeframe), "")
    universe = [u.value for u in hypothesis.suggested_universe]
    return StrategySpec(
        strategy_family=hypothesis.strategy_family,
        edge_mechanism=hypothesis.edge_mechanism,
        entry_logic=hypothesis.entry_logic,
        exit_mechanism=hypothesis.exit_mechanism,
        regime_assumption=hypothesis.regime_assumption,
        filters=list(hypothesis.filters),
        timeframe=timeframe,
        universe=universe,
        asset_specific=hypothesis.asset_specific,
    )


def validate_hypothesis(
    hypothesis: ResearcherHypothesis,
    prior_art: list[PriorArtMatch],
) -> None:
    """Reject a hypothesis that overlaps with prior art without addressing it,
    or whose first-iteration universe is too narrow (Plan 009 WP3).

    Rule (design §6.4): if `lookup_prior_art` returned matches, the Researcher
    MUST list every match in `differentiates_from`. Slugs in `differentiates_from`
    that don't appear in the prior-art set are also rejected (LLM made them up).

    Universe rule (WP3): a hypothesis must open on >= 3 assets so the phase-1
    funnel has something to narrow from — a single asset_class scope satisfies
    this (a class covers many symbols). The exception is an explicitly
    `asset_specific` edge (e.g. the DAX opening auction), which then requires a
    non-empty `asset_specific_rationale`.
    """
    _validate_universe_breadth(hypothesis)

    if not prior_art:
        return

    prior_slugs = {m.slug for m in prior_art}
    diff_slugs = set(hypothesis.differentiates_from)

    if not diff_slugs:
        raise HypothesisRejectedError(
            f"prior-art exists ({sorted(prior_slugs)}) but differentiates_from is empty"
        )

    missing = prior_slugs - diff_slugs
    if missing:
        raise HypothesisRejectedError(
            f"differentiates_from must address all prior art; missing {sorted(missing)}"
        )

    unknown = diff_slugs - prior_slugs
    if unknown:
        raise HypothesisRejectedError(
            f"differentiates_from references unknown slugs {sorted(unknown)}"
        )


def _validate_universe_breadth(hypothesis: ResearcherHypothesis) -> None:
    """Enforce the WP3 first-iteration universe rule (see validate_hypothesis)."""
    if hypothesis.asset_specific:
        if not hypothesis.asset_specific_rationale.strip():
            raise HypothesisRejectedError(
                "asset_specific=True requires a non-empty asset_specific_rationale "
                "(why the edge is mechanically bound to one instrument)"
            )
        return
    universe = hypothesis.suggested_universe
    has_class = any(u.scope == "asset_class" for u in universe)
    n_symbols = sum(1 for u in universe if u.scope == "symbol")
    if not has_class and n_symbols < 3:
        raise HypothesisRejectedError(
            "first-iteration universe must cover >= 3 assets (add symbols or an "
            "asset_class scope); set asset_specific=True with a rationale only if "
            f"the edge is bound to one instrument (got {n_symbols} symbol(s), "
            "no asset_class scope)"
        )


def _sanitize_family(strategy_family: str) -> str:
    """Normalize a strategy family string to lowercase alphanumerics only."""
    return re.sub(r"[^a-z0-9]", "", strategy_family.lower())


def _sanitize_asset_class(asset_class: str) -> str:
    """Normalize an asset class string to lowercase alphanumerics only."""
    return re.sub(r"[^a-z0-9]", "", asset_class.lower())


async def generate_slug(
    session: AsyncSession,
    strategy_family: str,
    asset_class: str | None,
) -> str:
    """Return next available `<family>__<asset>__<NNN>` slug, deterministically.

    Scans existing strategies for the same (family, asset_class) pair, finds the
    max NNN suffix, returns max+1 (or 001 if none). Unrelated slugs are ignored.
    `asset_class=None` uses the segment "agnostic".
    """
    family = _sanitize_family(strategy_family)
    asset = _sanitize_asset_class(asset_class) if asset_class else "agnostic"
    prefix = f"{family}__{asset}__"

    rows = (
        (await session.execute(select(Strategy.slug).where(Strategy.slug.like(f"{prefix}%"))))
        .scalars()
        .all()
    )

    max_n = 0
    for slug in rows:
        m = _SLUG_SUFFIX_RE.search(slug)
        if m:
            n = int(m.group(1))
            if n > max_n:
                max_n = n

    return f"{prefix}{max_n + 1:03d}"


async def generate_child_slug(session: AsyncSession, parent_slug: str) -> str:
    """Return `<base>__it00N` for the next iteration of `parent_slug`.

    A root parent (`orb__forex__001`) yields `orb__forex__001__it002`; a child
    parent (`orb__forex__001__it002`) yields `orb__forex__001__it003`. If the
    candidate slug is already taken (e.g. reiterate ran twice on the same
    parent), the number is bumped until free.
    """
    m = _ITER_SUFFIX_RE.search(parent_slug)
    if m:
        base = parent_slug[: m.start()]
        n = int(m.group(1)) + 1
    else:
        base = parent_slug
        n = 2

    while True:
        candidate = f"{base}__it{n:03d}"
        taken = (
            await session.execute(select(Strategy.id).where(Strategy.slug == candidate))
        ).scalar_one_or_none()
        if taken is None:
            return candidate
        n += 1
