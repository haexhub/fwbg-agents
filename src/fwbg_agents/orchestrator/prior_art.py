"""Tag-based prior-art lookup for the Researcher (M4).

Deterministic, no LLM. Researcher MUST call this before producing a hypothesis;
`validate_hypothesis` (orchestrator/hypotheses.py) refuses any hypothesis that
overlaps with existing strategies but does not declare `differentiates_from`.

Layer 1 (this module): Jaccard tag-similarity + same-family bypass.
Layer 2 (sqlite-vec embedding similarity): deferred to post-M4 — only worth
adding once the tag layer has been validated against real abandoned strategies.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fwbg_agents.persistence.models import Strategy, StrategyTag

JACCARD_THRESHOLD = 0.2
MAX_RESULTS = 20
POST_MORTEM_SUMMARY_CHARS = 240


class PriorArtMatch(BaseModel):
    """A prior strategy that overlaps with a new hypothesis, returned by lookup_prior_art."""

    slug: str
    current_state: str
    strategy_family: str
    asset_class: str | None  # None for asset-agnostic strategies
    tags_overlap: list[str]
    jaccard: float
    post_mortem_path: str | None = None
    post_mortem_summary: str | None = None


def _jaccard(a: set[str], b: set[str]) -> float:
    """Compute Jaccard similarity between two tag sets."""
    if not a and not b:
        return 0.0
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


def _load_summary(path_str: str | None) -> str | None:
    """Load and truncate the post-mortem summary text from a file path."""
    if not path_str:
        return None
    path = Path(path_str)
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    return text[:POST_MORTEM_SUMMARY_CHARS]


async def lookup_prior_art(
    session: AsyncSession,
    strategy_family: str,
    asset_class: str | None,
    tags: list[str],
) -> list[PriorArtMatch]:
    """Return prior strategies whose tags overlap with the candidate's.

    For class-pinned research (asset_class set), only same-class strategies are
    scanned to avoid penalising unrelated families in other markets.

    For asset-agnostic research (asset_class=None or ""), ALL strategies are
    scanned — otherwise asset-agnostic strategies would never see each other and
    the anti-redundancy gate would be blind to same-family repeats. The LLM
    passes "" for asset-agnostic; the DB stores None — both are normalised here.

    A strategy with the same `strategy_family` is always included even if its
    tag overlap is below threshold. Sorted by descending Jaccard, same-family
    as tiebreaker.
    """
    # Normalise: LLM passes "" for asset-agnostic; DB stores None.
    if asset_class == "":
        asset_class = None

    input_tags = set(tags)

    if asset_class is not None:
        stmt = select(Strategy).where(Strategy.asset_class == asset_class)
    else:
        stmt = select(Strategy)

    rows = (await session.execute(stmt)).scalars().all()
    if not rows:
        return []

    strategy_ids = [s.id for s in rows]
    tag_rows = (
        (
            await session.execute(
                select(StrategyTag).where(StrategyTag.strategy_id.in_(strategy_ids))
            )
        )
        .scalars()
        .all()
    )
    tags_by_strategy: dict[int, set[str]] = {sid: set() for sid in strategy_ids}
    for tr in tag_rows:
        tags_by_strategy[tr.strategy_id].add(tr.tag)

    matches: list[PriorArtMatch] = []
    for s in rows:
        found_tags = tags_by_strategy[s.id]
        jaccard = _jaccard(input_tags, found_tags)
        same_family = s.strategy_family == strategy_family
        if jaccard < JACCARD_THRESHOLD and not same_family:
            continue
        overlap = sorted(input_tags & found_tags)
        matches.append(
            PriorArtMatch(
                slug=s.slug,
                current_state=s.current_state,
                strategy_family=s.strategy_family,
                asset_class=s.asset_class,
                tags_overlap=overlap,
                jaccard=jaccard,
                post_mortem_path=s.post_mortem_path,
                post_mortem_summary=_load_summary(s.post_mortem_path),
            )
        )

    matches.sort(
        key=lambda m: (m.jaccard, 1 if m.strategy_family == strategy_family else 0),
        reverse=True,
    )
    return matches[:MAX_RESULTS]
