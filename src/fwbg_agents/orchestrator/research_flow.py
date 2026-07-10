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

import asyncio
import json
import logging
from datetime import UTC, datetime
from pathlib import Path

from pydantic_ai.models import Model
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fwbg_agents.agents.researcher import Researcher, ResearcherInput
from fwbg_agents.agents.translator import Translator
from fwbg_agents.config import settings
from fwbg_agents.orchestrator.hypotheses import (
    ResearcherHypothesis,
    generate_slug,
)
from fwbg_agents.orchestrator.lifecycle import strategy_dir
from fwbg_agents.orchestrator.lineage import generation_depth
from fwbg_agents.orchestrator.live_catalog import fetch_live_catalog, researcher_summary
from fwbg_agents.persistence.database import SessionLocal
from fwbg_agents.persistence.models import (
    Strategy,
    StrategyState,
    StrategyTag,
    Transition,
)
from fwbg_agents.tools.api_errors import describe_api_error
from fwbg_agents.tools.fwbg_client import (
    FwbgClient,
    FwbgClientError,
    safe_fwbg_strategy_name,
)
from fwbg_agents.tools.search import SearchProvider

log = logging.getLogger(__name__)


class ReiteratePreconditionError(ValueError):
    """Raised by `reiterate` when the parent isn't in a state suitable for
    re-iteration (not BACKTESTED, or missing Analyst sidecar)."""


class ResearcherFanoutExhaustedError(RuntimeError):
    """Raised by `research_and_translate` when every fan-out candidate
    failed (validation rejection or otherwise) within the same call."""


async def _generate_valid_hypothesis(
    input: ResearcherInput,
    *,
    model: Model | None,
    search_client: SearchProvider | None,
    fanout_n: int,
    available_plugins: dict | None = None,
) -> ResearcherHypothesis:
    """Run up to `fanout_n` Researcher attempts sequentially. Returns the
    first result that passes `validate_hypothesis`; on failure the next
    attempt starts immediately. Raises ResearcherFanoutExhaustedError when
    all attempts are exhausted.
    """

    async def _one_candidate() -> ResearcherHypothesis:
        """Run a single Researcher attempt and return the resulting hypothesis."""
        async with SessionLocal() as candidate_session:
            researcher = Researcher(
                candidate_session,
                model=model,
                search_client=search_client,
                available_plugins=available_plugins,
            )
            return await researcher.run(input)

    errors: list[BaseException] = []
    for attempt in range(1, fanout_n + 1):
        try:
            return await _one_candidate()
        except asyncio.CancelledError:
            # A user cancel (run_registry) must actually stop the flow — do
            # NOT swallow it as a failed attempt and spin up the next candidate.
            raise
        except Exception as exc:
            log.warning("researcher attempt %d/%d failed: %s", attempt, fanout_n, exc)
            errors.append(exc)

    reasons = "; ".join(describe_api_error(e) for e in errors)
    raise ResearcherFanoutExhaustedError(f"all {fanout_n} attempts failed: {reasons}")


def _render_research_notes(hypothesis: ResearcherHypothesis) -> str:
    """Render a ResearcherHypothesis to a human-readable Markdown notes string."""
    def _source_md(s) -> str:
        """Format a single Source as a Markdown bullet with nested key points."""
        lines = [f"- [{s.title}]({s.url}) — {s.why_relevant}"]
        for kp in s.key_points:
            lines.append(f"  - {kp}")
        return "\n".join(lines)

    sources_md = "\n".join(_source_md(s) for s in hypothesis.sources)
    diff_md = (
        "\n".join(f"- {slug}" for slug in hypothesis.differentiates_from)
        or "_(no prior art surfaced)_"
    )
    universe_md = (
        "\n".join(
            f"- **{u.scope}** `{u.value}`"
            + (f" ({u.timeframe})" if u.timeframe else "")
            + f" — {u.rationale}"
            for u in hypothesis.suggested_universe
        )
        or "_(no specific universe suggested)_"
    )
    model_kb_note = (
        "\n> ⚠️ Model-knowledge only — no live web search was available.\n"
        if hypothesis.model_knowledge_only
        else ""
    )
    return (
        f"# Research Notes — {hypothesis.title}\n\n"
        + model_kb_note
        + "\n## Hypothesis\n\n"
        f"{hypothesis.hypothesis.strip()}\n\n"
        "## Expected Edge\n\n"
        f"{hypothesis.expected_edge_explanation.strip()}\n\n"
        "## Key Indicators\n\n"
        + "\n".join(f"- `{ind}`" for ind in hypothesis.key_indicators)
        + "\n\n"
        "## Tags\n\n"
        + ", ".join(f"`{t}`" for t in hypothesis.tags)
        + "\n\n"
        "## Suggested Universe\n\n"
        f"{universe_md}\n\n"
        "## Differentiates From\n\n"
        f"{diff_md}\n\n"
        "## Sources\n\n"
        f"{sources_md}\n"
    )


async def publish_strategy_to_fwbg(
    session: AsyncSession,
    strategy: Strategy,
    strategy_path: Path,
    *,
    fwbg_client: FwbgClient | None = None,
) -> str | None:
    """Create the translated strategy.json as a NEW strategy in fwbg.

    Uses POST /api/strategies, which 409s on an existing name — nothing is
    ever overwritten. On a name collision (stale file from a wiped DB, …) a
    `_vN` suffix is tried so a fresh strategy is created regardless. The
    resulting fwbg filename is recorded in `strategy.metadata_json` so the
    Runner and the dashboard can point at the same file.

    Non-fatal by design: if fwbg is unreachable the research result must not
    be lost, so this logs a warning and returns None (the Runner re-publishes
    before the next backtest).
    """
    client = fwbg_client if fwbg_client is not None else FwbgClient(base_url=settings.fwbg_api_url)
    base_name = safe_fwbg_strategy_name(strategy.slug, 1)
    try:
        payload = json.loads(strategy_path.read_text())
        candidates = [base_name] + [f"{base_name}_v{n}" for n in range(2, 6)]
        for name in candidates:
            try:
                created = await client.create_strategy(name, payload)
            except FwbgClientError as exc:
                if exc.status == 409:
                    continue
                raise
            filename = created.get("filename", name)
            strategy.metadata_json = {
                **(strategy.metadata_json or {}),
                "fwbg_strategy_name": filename,
            }
            strategy.updated_at = datetime.now(UTC)
            await session.commit()
            log.info("published strategy %s to fwbg as %r", strategy.slug, filename)
            return filename
        log.warning(
            "could not publish %s to fwbg: all name candidates taken (%s..%s)",
            strategy.slug, candidates[0], candidates[-1],
        )
        return None
    except Exception:
        log.warning(
            "could not publish %s to fwbg (non-fatal; runner will retry)",
            strategy.slug,
            exc_info=True,
        )
        return None
    finally:
        if fwbg_client is None:
            await client.aclose()


async def research_and_translate(
    session: AsyncSession,
    input: ResearcherInput,
    *,
    model: Model | None = None,
    search_client: SearchProvider | None = None,
    fanout_n: int = 1,
    fwbg_client: FwbgClient | None = None,
) -> int:
    """Run Researcher (fanout_n candidates, first-valid-wins) → persist
    Strategy → run Translator (fresh).

    Returns the new Strategy id. The Researcher and Translator each manage
    their own AgentRun rows; this function is pure orchestration. Failures
    propagate (ResearcherFanoutExhaustedError / TranslatorError) — the
    caller is responsible for wrapping bookkeeping (e.g. the API
    background task).
    """
    client = fwbg_client if fwbg_client is not None else FwbgClient(
        base_url=settings.fwbg_api_url
    )
    try:
        return await _research_and_translate(
            session,
            input,
            model=model,
            search_client=search_client,
            fanout_n=fanout_n,
            fwbg_client=client,
        )
    finally:
        if fwbg_client is None:
            await client.aclose()


async def _research_and_translate(
    session: AsyncSession,
    input: ResearcherInput,
    *,
    model: Model | None,
    search_client: SearchProvider | None,
    fanout_n: int,
    fwbg_client: FwbgClient,
) -> int:
    """Run research fanout and translate the hypothesis into a persisted strategy.

    Returns the new strategy id."""
    # One live-catalog fetch per research run: the Researcher must see the
    # CURRENT plugin set (it grows as plugins are adopted), not a frozen list.
    live = await fetch_live_catalog(session, fwbg_client)
    hypothesis = await _generate_valid_hypothesis(
        input,
        model=model,
        search_client=search_client,
        fanout_n=fanout_n,
        available_plugins=researcher_summary(live),
    )

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
        suggested_universe=[u.model_dump() for u in hypothesis.suggested_universe],
        model_knowledge_only=hypothesis.model_knowledge_only,
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

    translator = Translator(session, model=model, fwbg_client=fwbg_client)
    strategy_path = await translator.run_fresh(strategy)

    # Register the finished strategy in fwbg right away so it shows up on the
    # dashboard's /strategy page, editable and startable before any backtest.
    await publish_strategy_to_fwbg(
        session, strategy, strategy_path, fwbg_client=fwbg_client
    )

    return strategy.id


async def reiterate(
    session: AsyncSession,
    parent_id: int,
    *,
    model: Model | None = None,
    fwbg_client: FwbgClient | None = None,
) -> int:
    """Apply Analyst sidecar to create a child Strategy. Returns child id.

    Preconditions (raise `ReiteratePreconditionError`):
    - Parent must exist.
    - Parent must be in BACKTESTED state.
    - Parent must be below `settings.reiterate_max_depth` in its chain.
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

    depth = await generation_depth(session, parent)
    if depth >= settings.reiterate_max_depth:
        raise ReiteratePreconditionError(
            f"parent {parent.slug} is at generation depth {depth}; "
            f"reiterate_max_depth={settings.reiterate_max_depth} reached — "
            "the chain must end in promote or abandon"
        )

    sidecar = strategy_dir(parent.slug) / "iteration_001" / "analyst_recommendation.json"
    if not sidecar.is_file():
        raise ReiteratePreconditionError(
            f"missing analyst_recommendation.json for {parent.slug} "
            f"at {sidecar}; run /strategies/{parent_id}/analyze first"
        )

    client = fwbg_client if fwbg_client is not None else FwbgClient(
        base_url=settings.fwbg_api_url
    )
    try:
        translator = Translator(session, model=model, fwbg_client=client)
        child = await translator.run_reiterate(parent)

        child_path = strategy_dir(child.slug) / "iteration_001" / "strategy.json"
        await publish_strategy_to_fwbg(session, child, child_path, fwbg_client=client)
    finally:
        if fwbg_client is None:
            await client.aclose()

    return child.id
