"""Orchestration glue for the Researcher → Translator pipeline (M4).

Wires the two M4 agents into the lifecycle:

- `research_and_translate(...)`: runs the Researcher to produce a hypothesis,
  persists a fresh Strategy row (PROPOSED, iteration_count=1) plus tags +
  initial Transition, writes `hypothesis.json` + `research_notes.md` into
  `data/strategies/<slug>/iteration_001/`, then runs the Translator
  (fresh-mode) to write `strategy.json` + `spec.md` alongside.

- `reiterate(...)`: precondition-checks a parent Strategy (must be
  BACKTESTED with an Analyst sidecar) and runs Translator.run_reiterate.
  Returns the child Strategy id.

Both functions are intentionally thin — heavy lifting lives in the
Researcher / Translator. They exist so the API layer can stay flat and
the smoke script can drive the pipeline without re-implementing it.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

from pydantic_ai.models import Model
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fwbg_agents.agents.researcher import Researcher, ResearcherInput
from fwbg_agents.agents.translator import Translator
from fwbg_agents.orchestrator.hypotheses import (
    ResearcherHypothesis,
    generate_slug,
)
from fwbg_agents.orchestrator.lifecycle import strategy_dir
from fwbg_agents.persistence.models import (
    Strategy,
    StrategyState,
    StrategyTag,
    Transition,
)
from fwbg_agents.tools.web_search import TavilyClient

log = logging.getLogger(__name__)


class ReiteratePreconditionError(ValueError):
    """Raised by `reiterate` when the parent isn't in a state suitable for
    re-iteration (not BACKTESTED, or missing Analyst sidecar)."""


def _render_research_notes(hypothesis: ResearcherHypothesis) -> str:
    sources_md = "\n".join(
        f"- [{s.title}]({s.url}) — {s.why_relevant}" for s in hypothesis.sources
    )
    diff_md = (
        "\n".join(f"- {slug}" for slug in hypothesis.differentiates_from)
        or "_(no prior art surfaced)_"
    )
    return (
        f"# Research Notes — {hypothesis.title}\n\n"
        "## Hypothesis\n\n"
        f"{hypothesis.hypothesis.strip()}\n\n"
        "## Expected Edge\n\n"
        f"{hypothesis.expected_edge_explanation.strip()}\n\n"
        "## Key Indicators\n\n"
        + "\n".join(f"- `{ind}`" for ind in hypothesis.key_indicators)
        + "\n\n"
        "## Tags\n\n"
        + ", ".join(f"`{t}`" for t in hypothesis.tags)
        + "\n\n"
        "## Differentiates From\n\n"
        f"{diff_md}\n\n"
        "## Sources\n\n"
        f"{sources_md}\n"
    )


async def research_and_translate(
    session: AsyncSession,
    input: ResearcherInput,
    *,
    model: Model | None = None,
    tavily: TavilyClient | None = None,
) -> int:
    """Run Researcher → persist Strategy → run Translator (fresh).

    Returns the new Strategy id. The Researcher and Translator each manage
    their own AgentRun rows; this function is pure orchestration. Failures
    propagate (ResearcherFailed / TranslatorFailed) — the caller is
    responsible for wrapping bookkeeping (e.g. the API background task).
    """
    researcher = Researcher(session, model=model, tavily=tavily)
    hypothesis = await researcher.run(input)

    slug = await generate_slug(
        session, hypothesis.strategy_family, hypothesis.asset_class
    )

    now = datetime.now(UTC)
    strategy = Strategy(
        slug=slug,
        current_state=StrategyState.PROPOSED.value,
        iteration_count=1,
        asset_class=hypothesis.asset_class,
        strategy_family=hypothesis.strategy_family,
        created_at=now,
        updated_at=now,
    )
    session.add(strategy)
    await session.flush()

    for tag in dict.fromkeys(hypothesis.tags):
        session.add(StrategyTag(strategy_id=strategy.id, tag=tag))

    iteration_dir = strategy_dir(slug) / "iteration_001"
    iteration_dir.mkdir(parents=True, exist_ok=True)
    hypothesis_path = iteration_dir / "hypothesis.json"
    hypothesis_path.write_text(hypothesis.model_dump_json(indent=2))
    (iteration_dir / "research_notes.md").write_text(
        _render_research_notes(hypothesis)
    )

    strategy.hypothesis_path = str(hypothesis_path)
    strategy.updated_at = datetime.now(UTC)

    session.add(
        Transition(
            entity_type="strategy",
            entity_id=strategy.id,
            from_state=None,
            to_state=StrategyState.PROPOSED.value,
            reason=f"researcher: {hypothesis.title}",
            payload={
                "hypothesis_title": hypothesis.title,
                "differentiates_from": list(hypothesis.differentiates_from),
            },
            created_by="researcher",
            created_at=datetime.now(UTC),
        )
    )
    await session.commit()
    await session.refresh(strategy)

    translator = Translator(session, model=model)
    await translator.run_fresh(strategy)

    return strategy.id


async def reiterate(
    session: AsyncSession,
    parent_id: int,
    *,
    model: Model | None = None,
) -> int:
    """Apply Analyst sidecar to create a child Strategy. Returns child id.

    Preconditions (raise `ReiteratePreconditionError`):
    - Parent must exist.
    - Parent must be in BACKTESTED state.
    - Parent must have an `analyst_recommendation.json` sidecar at
      `data/strategies/<slug>/iteration_001/`.
    """
    parent = (
        await session.execute(select(Strategy).where(Strategy.id == parent_id))
    ).scalar_one_or_none()
    if parent is None:
        raise ReiteratePreconditionError(f"parent strategy {parent_id} not found")

    if parent.current_state != StrategyState.BACKTESTED.value:
        raise ReiteratePreconditionError(
            f"parent {parent.slug} is in state {parent.current_state!r}; "
            "reiterate requires BACKTESTED"
        )

    sidecar = strategy_dir(parent.slug) / "iteration_001" / "analyst_recommendation.json"
    if not sidecar.is_file():
        raise ReiteratePreconditionError(
            f"missing analyst_recommendation.json for {parent.slug} "
            f"at {sidecar}; run /strategies/{parent_id}/analyze first"
        )

    translator = Translator(session, model=model)
    child = await translator.run_reiterate(parent)
    return child.id
