"""Research register card per lineage and the per-run note (Plan 028 D1/D2).

fwbg counts every backtest as a trial of a research card. One card stands for
one hypothesis, so the whole lineage (root and every reiterated child) shares
the root's card: its id lives only on the root, in
``Strategy.metadata_json["research_id"]``, and is created lazily on the
lineage's first run. The card's cutoff is the frozen lineage boundary, which
already separates iterations from the holdout the same way the register does.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Annotated, Any, Protocol

from pydantic import BaseModel, Field, StringConstraints, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fwbg_agents.orchestrator.lifecycle import strategy_dir
from fwbg_agents.orchestrator.lineage_boundary import get_or_freeze_boundary, lineage_root
from fwbg_agents.persistence.models import Strategy

log = logging.getLogger(__name__)

NOTE_MAX_CHARS = 240

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
    card needs none of the fields that changed since. A file without an
    ``edge_mechanism`` still fails here, since fwbg requires a mechanism.
    """

    title: _NonBlank
    tags: list[_NonBlank] = Field(min_length=1)
    hypothesis: _NonBlank
    edge_mechanism: _NonBlank


# ponytail: process-local locks keyed by root slug; they serialise card creation
# only within one process. A multi-process deployment would need a DB-level
# claim (e.g. a unique pending-card row) instead.
_card_locks: dict[str, asyncio.Lock] = {}


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

    async with _card_locks.setdefault(root.slug, asyncio.Lock()):
        # Another caller may have created the card while this one waited; reload
        # so a later metadata write from this session cannot drop the id.
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
            mechanism=card_input.edge_mechanism,
            cutoff=cutoff,
        )
        research_id = _card_id(response, root.slug)

        # Reassign rather than mutate: JSON columns do not track in-place edits.
        root.metadata_json = {**(root.metadata_json or {}), "research_id": research_id}
        await session.commit()

    if "warning" in response:
        # fwbg wrote the card but did not commit it; retrying would create a
        # second card, so the id is kept and the operator has to look.
        log.warning(
            "research_card: card %s for lineage %s created with warning: %s",
            research_id,
            root.slug,
            response["warning"],
        )
    return research_id


def _load_card_hypothesis(root_slug: str) -> _CardHypothesis:
    path = strategy_dir(root_slug) / "iteration_001" / "hypothesis.json"
    try:
        return _CardHypothesis.model_validate_json(path.read_text())
    except OSError as exc:
        raise ResearchCardError(f"cannot read hypothesis.json of {root_slug}: {exc}") from exc
    except ValidationError as exc:
        raise ResearchCardError(f"invalid hypothesis.json of {root_slug}: {exc}") from exc


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
            suffix += f": {', '.join(assets)}"
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
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        log.warning("research_card: unreadable %s; using a generic note", path, exc_info=True)
        return None
    return data if isinstance(data, dict) else None


def _one_line(text: str) -> str:
    return " ".join(text.split())
