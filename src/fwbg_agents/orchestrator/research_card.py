"""Research register card per lineage and the per-run note (Plan 028 D1/D2).

fwbg counts every backtest as a trial of a research card. One card stands for
one hypothesis, so the whole lineage (root and every reiterated child) shares
the root's card: its id lives only on the root, in
``Strategy.metadata_json["research_id"]``, and is created lazily on the
lineage's first run. An auto-created card's cutoff is the frozen lineage
boundary B; from then on the card's cutoff is the source of truth for the
split (iterations send no end_date, fwbg clamps them to it).
"""

from __future__ import annotations

import asyncio
import json
import logging
import weakref
from pathlib import Path
from typing import Annotated, Any, Protocol

from pydantic import BaseModel, Field, StringConstraints, ValidationError, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fwbg_agents.orchestrator.lifecycle import strategy_dir
from fwbg_agents.orchestrator.lineage_boundary import get_or_freeze_boundary, lineage_root
from fwbg_agents.persistence.models import Strategy

log = logging.getLogger(__name__)

NOTE_MAX_CHARS = 240
# Few enough that a universe suffix leaves most of the note to the reason.
_NOTE_MAX_ASSETS = 5

_NonBlank = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class ResearchCardError(Exception):
    """The lineage root has no usable hypothesis, or fwbg answered without a card id."""


class _CardClient(Protocol):
    async def create_research_card(
        self, *, title: str, tags: list[str], hypothesis: str, mechanism: str, cutoff: str
    ) -> dict[str, Any]: ...


class _CardHypothesis(BaseModel):
    """The slice of ``hypothesis.json`` a card is built from.

    Not ``ResearcherHypothesis``: files written before Plan 009 carry a
    free-text ``strategy_family`` that the current vocabulary rejects, and the
    card needs none of the fields that changed since. Those files also predate
    ``edge_mechanism``; their ``expected_edge_explanation`` states the same
    thing (why the edge exists), so it stands in. With neither, the file fails
    here, since fwbg requires a mechanism.
    """

    title: _NonBlank
    tags: list[_NonBlank] = Field(min_length=1)
    hypothesis: _NonBlank
    edge_mechanism: str = ""
    expected_edge_explanation: str = ""

    @model_validator(mode="after")
    def _has_mechanism(self) -> _CardHypothesis:
        if not self.mechanism:
            raise ValueError("needs a non-blank edge_mechanism or expected_edge_explanation")
        return self

    @property
    def mechanism(self) -> str:
        return self.edge_mechanism.strip() or self.expected_edge_explanation.strip()


# ponytail: process-local locks keyed by root slug; they serialise card creation
# only within one process. A multi-process deployment would need a DB-level
# claim (e.g. a unique pending-card row) instead. Weak values: a lock lives only
# while someone holds or awaits it, so the map cannot grow without bound.
_card_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()


async def ensure_research_id(session: AsyncSession, strategy: Strategy, fwbg: _CardClient) -> str:
    """Return the research card id of ``strategy``'s lineage, creating the card once.

    A newly created id is committed right away (together with anything else
    pending in ``session``): the card exists in fwbg from that moment, and a
    lost id would make the next run create a second card for the same idea.

    Raises ``ResearchCardError`` when the root's hypothesis is missing or
    invalid (no card is invented) or fwbg's response carries no card id.
    """
    root = await lineage_root(session, strategy)
    research_id = (root.metadata_json or {}).get("research_id")
    if research_id:
        return str(research_id)

    lock = _card_locks.setdefault(root.slug, asyncio.Lock())
    async with lock:
        # Another caller may have created the card while this one waited; reload
        # so a later metadata write from this session cannot drop the id. The
        # flush first writes a pending metadata change the refresh would discard.
        await session.flush()
        await session.refresh(root, attribute_names=["metadata_json"])
        research_id = (root.metadata_json or {}).get("research_id")
        if research_id:
            return str(research_id)

        card_input = _load_card_hypothesis(root.slug)
        cutoff = await get_or_freeze_boundary(session, root)
        response = await fwbg.create_research_card(
            title=card_input.title,
            tags=card_input.tags,
            hypothesis=card_input.hypothesis,
            mechanism=card_input.mechanism,
            cutoff=cutoff,
        )
        research_id = _card_id(response, root.slug)
        # Logged before the commit: should the commit fail, the card exists in
        # fwbg and its id must still be findable.
        log.info("research_card: created %s for lineage %s", research_id, root.slug)
        if "warning" in response:
            # fwbg wrote the card but did not commit it; retrying would create a
            # second card, so the id is kept and the operator has to look.
            log.warning(
                "research_card: card %s for lineage %s created with warning: %s",
                research_id,
                root.slug,
                response["warning"],
            )

        # Reassign rather than mutate: JSON columns do not track in-place edits.
        root.metadata_json = {**(root.metadata_json or {}), "research_id": research_id}
        await session.commit()
    return research_id


def _load_card_hypothesis(root_slug: str) -> _CardHypothesis:
    path = strategy_dir(root_slug) / "iteration_001" / "hypothesis.json"
    remedy = (
        f"lineage {root_slug} has no research card; add a valid {path} (new strategies "
        "can instead be created via POST /strategies with a research_id)"
    )
    try:
        return _CardHypothesis.model_validate_json(path.read_bytes())
    except OSError as exc:
        raise ResearchCardError(f"{remedy} (cannot read hypothesis.json: {exc})") from exc
    except ValidationError as exc:
        raise ResearchCardError(f"{remedy} (invalid hypothesis.json: {exc})") from exc


def _card_id(response: dict[str, Any], root_slug: str) -> str:
    card = response.get("card")
    card_id = card.get("id") if isinstance(card, dict) else None
    if not isinstance(card_id, str) or not card_id:
        raise ResearchCardError(f"fwbg created no card id for lineage {root_slug}: {response}")
    return card_id


async def run_note(
    session: AsyncSession,
    strategy: Strategy,
    *,
    attempt_label: str | None = None,
    assets: list[str] | None = None,
) -> str:
    """Build the single-line register note for a backtest of ``strategy``.

    A root states its hypothesis title, a child the parent's recommendation it
    was built from. When that source file is missing or unreadable the note
    still names the strategy, because fwbg refuses an empty note. A universe
    attempt is appended (and survives truncation) so fallback attempts are
    told apart.
    """
    base = await _note_base(session, strategy)
    suffix = ""
    if attempt_label is not None:
        suffix = f"; universe {attempt_label}"
        if assets:
            suffix += f": {', '.join(assets[:_NOTE_MAX_ASSETS])}"
            if len(assets) > _NOTE_MAX_ASSETS:
                suffix += f" +{len(assets) - _NOTE_MAX_ASSETS} more"
        suffix = _one_line(suffix)
    head = _one_line(base)[: max(NOTE_MAX_CHARS - len(suffix), 0)].rstrip()
    return (head + suffix)[:NOTE_MAX_CHARS]


async def _note_base(session: AsyncSession, strategy: Strategy) -> str:
    if strategy.parent_strategy_id is None:
        hypothesis = _read_json(strategy_dir(strategy.slug) / "iteration_001" / "hypothesis.json")
        title = hypothesis.get("title") if hypothesis else None
        if isinstance(title, str) and title.strip():
            return f"initial backtest: {title}"
        return f"initial backtest of {strategy.slug}"

    parent = (
        await session.execute(select(Strategy).where(Strategy.id == strategy.parent_strategy_id))
    ).scalar_one_or_none()
    if parent is not None:
        # ponytail: the parent's recommendation file is overwritten when the
        # parent is analysed again, so a re-run child may be described by a newer
        # recommendation than the one it was built from. Snapshotting the reason
        # on the child at reiterate time would fix that.
        rec = _read_json(
            strategy_dir(parent.slug) / "iteration_001" / "analyst_recommendation.json"
        )
        kind = rec.get("kind") if rec else None
        reasoning = rec.get("reasoning") if rec else None
        if isinstance(kind, str) and isinstance(reasoning, str) and reasoning.strip():
            return f"{kind} from {parent.slug}: {reasoning}"
    return f"iteration of {strategy.slug}"


def _read_json(path: Path) -> dict[str, Any] | None:
    """Return the file's JSON object, or None when it is absent or unusable.

    Only feeds the note, which has a generic fallback; an unreadable file is
    logged rather than failing the run.
    """
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_bytes())
    except (OSError, ValueError):  # ValueError covers bad JSON and bad UTF-8
        log.warning("research_card: unreadable %s; using a generic note", path, exc_info=True)
        return None
    return data if isinstance(data, dict) else None


def _one_line(text: str) -> str:
    return " ".join(text.split())
