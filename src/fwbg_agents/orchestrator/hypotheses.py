"""Researcher hypothesis schema, validator, and deterministic slug generator (M4).

The pydantic models live here (not in researcher.py) so the validator and
slug generator can be imported without pulling in pydantic-ai or LLM clients.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fwbg_agents.orchestrator.prior_art import PriorArtMatch
from fwbg_agents.persistence.models import Strategy

_SLUG_SUFFIX_RE = re.compile(r"__(\d{3,})$")


class HypothesisRejectedError(ValueError):
    """Raised by validate_hypothesis when the Researcher output conflicts with prior art."""


class Source(BaseModel):
    url: str
    title: str
    why_relevant: str


class ResearcherHypothesis(BaseModel):
    """Structured output of the Researcher agent.

    `differentiates_from` lists slugs of prior strategies this hypothesis
    deliberately deviates from. Required by `validate_hypothesis` whenever
    `lookup_prior_art` returned matches.
    """

    title: str
    asset_class: str
    strategy_family: str
    hypothesis: str
    expected_edge_explanation: str
    key_indicators: list[str] = Field(min_length=1)
    tags: list[str] = Field(min_length=1)
    sources: list[Source] = Field(min_length=1)
    differentiates_from: list[str] = Field(default_factory=list)


def validate_hypothesis(
    hypothesis: ResearcherHypothesis,
    prior_art: list[PriorArtMatch],
) -> None:
    """Reject a hypothesis that overlaps with prior art without addressing it.

    Rule (design §6.4): if `lookup_prior_art` returned matches, the Researcher
    MUST list every match in `differentiates_from`. Slugs in `differentiates_from`
    that don't appear in the prior-art set are also rejected (LLM made them up).
    """
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


def _sanitize_family(strategy_family: str) -> str:
    return re.sub(r"[^a-z0-9]", "", strategy_family.lower())


def _sanitize_asset_class(asset_class: str) -> str:
    return re.sub(r"[^a-z0-9]", "", asset_class.lower())


async def generate_slug(
    session: AsyncSession,
    strategy_family: str,
    asset_class: str,
) -> str:
    """Return next available `<family>__<asset>__<NNN>` slug, deterministically.

    Scans existing strategies for the same (family, asset_class) pair, finds the
    max NNN suffix, returns max+1 (or 001 if none). Unrelated slugs are ignored.
    """
    family = _sanitize_family(strategy_family)
    asset = _sanitize_asset_class(asset_class)
    prefix = f"{family}__{asset}__"

    rows = (
        await session.execute(
            select(Strategy.slug).where(Strategy.slug.like(f"{prefix}%"))
        )
    ).scalars().all()

    max_n = 0
    for slug in rows:
        m = _SLUG_SUFFIX_RE.search(slug)
        if m:
            n = int(m.group(1))
            if n > max_n:
                max_n = n

    return f"{prefix}{max_n + 1:03d}"
