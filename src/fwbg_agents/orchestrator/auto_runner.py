"""Runner auto mode: backtest waiting strategies without a human trigger.

When enabled, two independent background loops run concurrently:
- `run_loop`: every `runner_auto_poll_seconds`, if no runner is active, picks
  the oldest PROPOSED strategy with a strategy.json and backtests it.
- `pipeline_fill_loop`: every `pipeline_fill_poll_seconds`, if the PROPOSED
  count is below `pipeline_min_proposed`, triggers a new research cycle.

The two loops are fully independent — research never blocks a backtest and
vice versa. fwbg's own 429 / single-slot enforcement handles any concurrency.

Deliberately single-flight: at most one backtest at a time (fwbg runs are
CPU-heavy). Strategies whose auto-triggered backtests already failed
`runner_auto_max_attempts` times are skipped so a broken strategy cannot
starve the queue by being retried forever; a manual run remains possible.

The on/off switch is persisted in the data dir (survives restarts) and
exposed via GET/PUT /agents/runner/auto.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime

import yaml
from sqlalchemy import and_, func, not_, nulls_last, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from fwbg_agents.agents.analyst import (
    Abandon,
    Analyst,
    ChangeExit,
    ModifyPlugins,
    TuneParams,
    _best_symbol_metrics_from_results,
    _median_metrics_across_assets,
)
from fwbg_agents.agents.researcher import ResearcherInput
from fwbg_agents.agents.runner import Runner, RunnerConfigError
from fwbg_agents.agents.translator import Translator
from fwbg_agents.config import settings
from fwbg_agents.orchestrator.lifecycle import strategy_dir, transition_strategy
from fwbg_agents.orchestrator.lineage import (
    family_strategies,
    generation_depth,
    has_metric_improvement,
)
from fwbg_agents.orchestrator.plugin_flow import (
    PluginAuthorError,
    _find_latest_sidecar,
    author_plugin_from_strategy,
    evaluate_plugin,
    reiterate_with_plugin,
)
from fwbg_agents.orchestrator.recommendations import validate_and_apply
from fwbg_agents.orchestrator.research_flow import (
    publish_strategy_to_fwbg,
    reiterate,
    research_and_translate,
)
from fwbg_agents.orchestrator.run_janitor import ORPHAN_ERROR, TRANSIENT_ERROR
from fwbg_agents.persistence.agent_runs import (
    fail_agent_run,
    finish_agent_run,
    start_agent_run,
)
from fwbg_agents.persistence.database import SessionLocal
from fwbg_agents.persistence.models import (
    AgentRun,
    AgentRunStatus,
    Plugin,
    PluginState,
    Setting,
    Strategy,
    StrategyState,
)
from fwbg_agents.tools.fwbg_client import FwbgClient
from fwbg_agents.tools.search import BraveClient, FallbackSearchClient, TavilyClient
from fwbg_agents.tools.secrets import get_secret

log = logging.getLogger(__name__)

_background_tasks: set[asyncio.Task] = set()


async def _get_setting(key: str, default: str) -> str:
    async with SessionLocal() as session:
        row = (
            await session.execute(select(Setting).where(Setting.key == key))
        ).scalar_one_or_none()
        return row.value if row is not None else default


async def _set_setting(key: str, value: str) -> None:
    async with SessionLocal() as session:
        row = (
            await session.execute(select(Setting).where(Setting.key == key))
        ).scalar_one_or_none()
        if row is None:
            session.add(Setting(key=key, value=value))
        else:
            row.value = value
        await session.commit()


async def is_enabled() -> bool:
    """Return True if auto-runner mode is currently enabled."""
    return (await _get_setting("runner_auto.enabled", "false")) == "true"


async def set_enabled(enabled: bool) -> None:
    """Enable or disable auto-runner mode and persist the change to state.db."""
    await _set_setting("runner_auto.enabled", "true" if enabled else "false")
    log.info("runner auto mode %s", "enabled" if enabled else "disabled")


async def get_pipeline_min_proposed() -> int:
    """Return the effective pipeline_min_proposed threshold from DB or settings default."""
    v = await _get_setting("runner_auto.pipeline_min_proposed", "")
    if not v:
        return settings.pipeline_min_proposed
    return max(0, min(int(v), 20))


async def set_pipeline_min_proposed(value: int) -> None:
    """Set and persist the pipeline_min_proposed threshold (clamped to 0-20)."""
    value = max(0, min(int(value), 20))
    await _set_setting("runner_auto.pipeline_min_proposed", str(value))
    log.info("pipeline_min_proposed set to %d", value)


_IN_FLIGHT_STATUSES = (AgentRunStatus.RUNNING.value, AgentRunStatus.PENDING.value)


async def _count_runs(
    session: AsyncSession,
    agent_name: str | tuple[str, ...],
    *,
    strategy_id: int | None = None,
    statuses: tuple[str, ...] | None = None,
) -> int:
    """Count AgentRun rows for the given agent name(s), optionally scoped to a
    strategy and/or a set of statuses."""
    names = (agent_name,) if isinstance(agent_name, str) else agent_name
    stmt = select(func.count()).select_from(AgentRun).where(AgentRun.agent_name.in_(names))
    if strategy_id is not None:
        stmt = stmt.where(AgentRun.strategy_id == strategy_id)
    if statuses is not None:
        stmt = stmt.where(AgentRun.status.in_(statuses))
    return (await session.execute(stmt)).scalar_one()


async def pick_next_strategy_id(session: AsyncSession) -> int | None:
    """Oldest PROPOSED strategy that is ready and worth auto-running.

    Returns None when a backtest is already active (single-flight) or no
    candidate qualifies.
    """
    busy = await _count_runs(session, "runner", statuses=_IN_FLIGHT_STATUSES)
    if busy:
        return None

    proposed = (
        (
            await session.execute(
                select(Strategy)
                .where(Strategy.current_state == StrategyState.PROPOSED.value)
                .order_by(nulls_last(Strategy.queue_position), Strategy.created_at)
            )
        )
        .scalars()
        .all()
    )

    for s in proposed:
        if not (strategy_dir(s.slug) / "iteration_001" / "strategy.json").is_file():
            continue  # not translated yet
        attempts = await _genuine_failed_attempts(session, "runner", s.id)
        if attempts >= settings.runner_auto_max_attempts:
            continue
        return s.id
    return None


async def _genuine_failed_attempts(session: AsyncSession, agent_name: str, strategy_id: int) -> int:
    """Failed attempts that count against a retry cap. Orphaned and
    transient-network failures were not the strategy's fault, so they are
    excluded."""
    return (
        await session.execute(
            select(func.count())
            .select_from(AgentRun)
            .where(
                AgentRun.agent_name == agent_name,
                AgentRun.strategy_id == strategy_id,
                AgentRun.status == AgentRunStatus.FAILED.value,
                or_(
                    AgentRun.error.is_(None),
                    and_(
                        AgentRun.error != ORPHAN_ERROR,
                        not_(AgentRun.error.like(f"{TRANSIENT_ERROR}%")),
                    ),
                ),
            )
        )
    ).scalar_one()


async def pick_next_backtested_unanalyzed(session: AsyncSession) -> int | None:
    """Oldest BACKTESTED strategy that has no successful Analyst run yet.

    Backfills strategies that were backtested while the Analyst was crashing
    (or before auto-analysis existed): without this they sit in BACKTESTED
    forever, never advancing and clogging the pipeline-fill "active" count.
    """
    rows = (
        (
            await session.execute(
                select(Strategy)
                .where(Strategy.current_state == StrategyState.BACKTESTED.value)
                .order_by(Strategy.created_at)
            )
        )
        .scalars()
        .all()
    )
    for s in rows:
        done = await _count_runs(
            session,
            "analyst",
            strategy_id=s.id,
            statuses=(AgentRunStatus.DONE.value,),
        )
        if done == 0 and (strategy_dir(s.slug) / "iteration_001" / "fwbg_results.json").is_file():
            return s.id
    return None


async def pick_next_add_indicator_pending(session: AsyncSession) -> int | None:
    """Oldest BACKTESTED strategy with an add_indicator sidecar and remaining
    auto plugin-author budget.

    Budget = plugin_planner runs that ended DONE plus genuine planner failures
    (orphaned/transient failures were not the strategy's fault and are free),
    compared against plugin_author_auto_max_attempts. Every chain attempt runs
    the planner first, so an implementer failure consumes one budget unit and
    the strategy is retried up to the cap. Any plugin_implementer DONE row
    closes the budget — authoring succeeded; evaluator/reiterate failures
    after that point need a manual retry via the API. A strategy whose chain
    is currently in flight (RUNNING/PENDING run, e.g. a manual API attempt) is
    skipped so two chains never race on the same sidecar. Manual API attempts
    are never blocked here, but their planner runs consume the same budget.
    """
    rows = (
        (
            await session.execute(
                select(Strategy)
                .where(Strategy.current_state == StrategyState.BACKTESTED.value)
                .order_by(Strategy.created_at)
            )
        )
        .scalars()
        .all()
    )
    for s in rows:
        if _find_latest_sidecar(s.slug) is None:
            continue
        implementer_done = await _count_runs(
            session,
            "plugin_implementer",
            strategy_id=s.id,
            statuses=(AgentRunStatus.DONE.value,),
        )
        if implementer_done > 0:
            continue
        planner_done = await _count_runs(
            session,
            "plugin_planner",
            strategy_id=s.id,
            statuses=(AgentRunStatus.DONE.value,),
        )
        planner_failed = await _genuine_failed_attempts(session, "plugin_planner", s.id)
        if planner_done + planner_failed >= settings.plugin_author_auto_max_attempts:
            continue
        in_flight = await _count_runs(
            session,
            ("plugin_planner", "plugin_implementer", "plugin_author_flow"),
            strategy_id=s.id,
            statuses=_IN_FLIGHT_STATUSES,
        )
        if in_flight:
            continue
        return s.id
    return None


async def _author_and_reiterate(session: AsyncSession, sid: int) -> None:
    """Drive one add_indicator sidecar through the full plugin chain:
    PluginPlanner → PluginImplementer → PluginEvaluator → reiterate-with-plugin.

    Any failure leaves the strategy in BACKTESTED with its plugin-author
    attempt consumed (AgentRun rows carry the post-mortem trail); a verified
    plugin ends with a child PROPOSED strategy that the auto-runner backtests
    on the next free slot.
    """
    try:
        plugin_id = await author_plugin_from_strategy(session, sid)
    except PluginAuthorError as exc:
        log.warning("runner auto mode: plugin author failed for strategy %s: %s", sid, exc)
        return
    except Exception:
        log.exception("runner auto mode: plugin author crashed for strategy %s", sid)
        return

    eval_ar = await start_agent_run(session, agent_name="plugin_evaluator", plugin_id=plugin_id)
    try:
        await evaluate_plugin(session, plugin_id, agent_run_id=eval_ar.id)
    except Exception as exc:
        log.exception("runner auto mode: plugin evaluation crashed for plugin %s", plugin_id)
        await fail_agent_run(session, eval_ar, exc)
        return
    plugin = (await session.execute(select(Plugin).where(Plugin.id == plugin_id))).scalar_one()
    verified = plugin.current_state == PluginState.VERIFIED.value
    # A non-verifying evaluation is a failed run, not a success: the evaluator
    # ran but rejected the plugin. Mark it FAILED so the runs list surfaces the
    # rejection instead of a misleading DONE (a genuine evaluator crash is
    # already handled above via fail_agent_run).
    await finish_agent_run(
        session,
        eval_ar,
        status=AgentRunStatus.DONE if verified else AgentRunStatus.FAILED,
        plugin_id=plugin_id,
        error=None if verified else f"plugin did not verify (state={plugin.current_state})",
    )
    if not verified:
        log.warning(
            "runner auto mode: plugin %s did not verify (state=%s); "
            "strategy %s stays in BACKTESTED",
            plugin.slug,
            plugin.current_state,
            sid,
        )
        return

    try:
        child_id = await reiterate_with_plugin(session, sid, plugin.slug)
        log.info(
            "runner auto mode: plugin %s verified; iteration queued as strategy %s (parent %s)",
            plugin.slug,
            child_id,
            sid,
        )
    except Exception:
        log.exception("runner auto mode: reiterate-with-plugin failed for strategy %s", sid)


async def abandon_capped_proposed(session: AsyncSession) -> int:
    """Abandon PROPOSED strategies that have exhausted their backtest retry
    budget. They can never be auto-picked again, so leaving them PROPOSED
    clogs the pipeline-fill "active" count and starves fresh research. Returns
    how many were abandoned."""
    proposed = (
        (
            await session.execute(
                select(Strategy).where(Strategy.current_state == StrategyState.PROPOSED.value)
            )
        )
        .scalars()
        .all()
    )
    abandoned = 0
    for s in proposed:
        has_strategy_json = (strategy_dir(s.slug) / "iteration_001" / "strategy.json").is_file()

        if has_strategy_json:
            failed = await _genuine_failed_attempts(session, "runner", s.id)
            cap = settings.runner_auto_max_attempts
            if failed < cap:
                continue
            reason = f"auto: exceeded backtest retry cap ({failed} attempts)"
            summary = (
                f"Auto-abandoned: {failed} backtest attempts failed "
                f"(cap={cap}); never reached a state the Analyst could evaluate."
            )
        else:
            failed = await _genuine_failed_attempts(session, "translator", s.id)
            cap = settings.translator_auto_max_attempts
            if failed < cap:
                continue
            reason = f"auto: exceeded translator retry cap ({failed} attempts)"
            summary = (
                f"Auto-abandoned: {failed} translator attempts failed "
                f"(cap={cap}); strategy.json was never produced."
            )

        pm_path = strategy_dir(s.slug) / "post_mortem.yaml"
        pm_path.parent.mkdir(parents=True, exist_ok=True)
        pm_path.write_text(
            yaml.safe_dump(
                {
                    "slug": s.slug,
                    "asset_class": s.asset_class,
                    "strategy_family": s.strategy_family,
                    "summary": summary,
                    "written_at": datetime.now(UTC).isoformat(),
                },
                sort_keys=False,
            )
        )
        try:
            await transition_strategy(
                session,
                s,
                StrategyState.ABANDONED,
                reason=reason,
                payload={"post_mortem_path": str(pm_path)},
                created_by="auto_runner",
            )
            abandoned += 1
            log.info("runner auto mode: abandoned capped strategy %s (%s)", s.id, reason)
        except Exception:
            log.exception("runner auto mode: could not abandon capped strategy %s", s.id)
    return abandoned


async def pick_next_untranslated_proposed(session: AsyncSession) -> int | None:
    """Oldest PROPOSED strategy that has a hypothesis.json but no strategy.json,
    with translator retries still available and no in-flight translator run.

    These are strategies where the research succeeded but the Translator crashed
    or produced an invalid strategy.json. The LLM is non-deterministic so a
    retry may succeed.
    """
    proposed = (
        (
            await session.execute(
                select(Strategy)
                .where(Strategy.current_state == StrategyState.PROPOSED.value)
                .order_by(Strategy.created_at)
            )
        )
        .scalars()
        .all()
    )
    for s in proposed:
        if (strategy_dir(s.slug) / "iteration_001" / "strategy.json").is_file():
            continue
        if not (strategy_dir(s.slug) / "iteration_001" / "hypothesis.json").is_file():
            continue
        failed = await _genuine_failed_attempts(session, "translator", s.id)
        if failed >= settings.translator_auto_max_attempts:
            continue
        in_flight = await _count_runs(
            session,
            "translator",
            strategy_id=s.id,
            statuses=_IN_FLIGHT_STATUSES,
        )
        if in_flight:
            continue
        return s.id
    return None


async def _retranslate(session: AsyncSession, sid: int) -> None:
    """Retry Translator.run_fresh on a PROPOSED strategy whose previous
    translation failed. The hypothesis.json is already on disk; a new
    translator AgentRun is created automatically by the Translator agent."""
    s = (await session.execute(select(Strategy).where(Strategy.id == sid))).scalar_one()
    client = FwbgClient(base_url=settings.fwbg_api_url)
    try:
        translator = Translator(session, fwbg_client=client)
        strategy_path = await translator.run_fresh(s)
        await publish_strategy_to_fwbg(session, s, strategy_path, fwbg_client=client)
        log.info("runner auto mode: retranslation succeeded for strategy %s", sid)
    except Exception:
        log.exception("runner auto mode: retranslation failed for strategy %s", sid)
    finally:
        await client.aclose()


def _synthesize_abandon_override_sidecar(slug: str, analyst_reasoning: str) -> dict:
    """Build a minimal tune_params sidecar from exit strategy params.

    Called when the Analyst recommends abandon before min_iterations_before_abandon.
    Extracts up to 3 numeric params from exit_strategies[0] and widens their
    ranges by ±30% so the next iteration explores the neighbourhood.
    Raises ValueError if no tunable params are found.
    """
    strat_path = strategy_dir(slug) / "iteration_001" / "strategy.json"
    strat = json.loads(strat_path.read_text())
    exits = strat.get("exit_strategies", [])
    exit_params = {
        k: v
        for k, v in (exits[0].get("params", {}) if exits else {}).items()
        if isinstance(v, (int, float))
    }
    if not exit_params:
        raise ValueError(f"no numeric exit params to tune for {slug}")
    tune_params = []
    for param, val in list(exit_params.items())[:3]:
        if val == 0:
            new_range = [0.5, 1.0, 1.5]
        else:
            new_range = [round(val * 0.7, 4), round(val, 4), round(val * 1.3, 4)]
        tune_params.append({"param": param, "new_range": new_range})
    return {
        "kind": "tune_params",
        "params": tune_params,
        "confidence": 0.3,
        "reasoning": (
            f"Early-abandon override "
            f"(depth < min_iterations_before_abandon={settings.min_iterations_before_abandon}). "
            f"Analyst reasoning: {analyst_reasoning}"
        ),
    }


async def _analyze_and_apply(session: AsyncSession, sid: int) -> None:
    """Run the Analyst on a backtested strategy, apply the recommendation, and
    queue an iteration for tune/change-exit. Shared by the fresh-backtest path
    and the backtested-backlog drain."""
    s = (await session.execute(select(Strategy).where(Strategy.id == sid))).scalar_one()
    client = FwbgClient(base_url=settings.fwbg_api_url)
    try:
        rec = await Analyst(session, fwbg_client=client).analyze(s)
    except Exception:
        log.exception("runner auto mode: analyst failed for strategy %s", sid)
        return
    finally:
        await client.aclose()

    results_path = strategy_dir(s.slug) / "iteration_001" / "fwbg_results.json"
    metrics: dict[str, float] = {}
    if results_path.is_file():
        results = json.loads(results_path.read_text())
        metrics = {
            k: float(v)
            for k, v in _median_metrics_across_assets(results).items()
            if isinstance(v, (int, float))
        }

    # Guard: override an Abandon recommendation when the strategy has not had a
    # fair chance yet.  Two independent conditions both trigger the same override:
    # (A) depth < min_iterations_before_abandon — too few iterations regardless
    #     of trend; and
    # (B) at least one core metric (Sharpe, profit_factor) is still improving
    #     across recent iterations — the strategy has momentum worth following.
    if isinstance(rec, Abandon):
        depth = await generation_depth(session, s)
        override_reason: str | None = None
        if depth < settings.min_iterations_before_abandon:
            override_reason = (
                f"depth={depth} < min_iterations_before_abandon="
                f"{settings.min_iterations_before_abandon}"
            )
        else:
            family = await family_strategies(session, s)
            metrics_history: list[dict] = []
            for member in family:
                rp = strategy_dir(member.slug) / "iteration_001" / "fwbg_results.json"
                if rp.is_file():
                    try:
                        metrics_history.append(
                            _best_symbol_metrics_from_results(json.loads(rp.read_text()))
                        )
                    except Exception:
                        metrics_history.append({})
                else:
                    metrics_history.append({})
            if has_metric_improvement(metrics_history):
                override_reason = "core metrics still improving across recent iterations"

        if override_reason:
            log.info(
                "runner auto mode: overriding abandon for strategy %s (%s) — forcing tune_params",
                s.slug,
                override_reason,
            )
            try:
                sidecar_data = _synthesize_abandon_override_sidecar(s.slug, rec.reasoning)
                iteration_dir = strategy_dir(s.slug) / "iteration_001"
                iteration_dir.mkdir(parents=True, exist_ok=True)
                (iteration_dir / "analyst_recommendation.json").write_text(
                    json.dumps(sidecar_data, indent=2)
                )
                child_id = await reiterate(session, sid)
                log.info(
                    "runner auto mode: abandon override — "
                    "iteration queued as strategy %d (parent %s)",
                    child_id,
                    s.slug,
                )
                return
            except Exception:
                log.exception(
                    "runner auto mode: abandon override failed for %s — falling through to abandon",
                    s.slug,
                )

    try:
        await validate_and_apply(session, s, rec, metrics=metrics)
    except Exception as exc:
        log.warning(
            "runner auto mode: analyst recommendation rejected for strategy %s: %s",
            sid,
            exc,
        )
        return

    # TuneParams / ChangeExit / ModifyPlugins → create a child PROPOSED
    # strategy so the auto-runner picks it up on the next free slot.
    # AddIndicator is drained by the plugin-author chain (see tick()).
    if isinstance(rec, (TuneParams, ChangeExit, ModifyPlugins)):
        depth = await generation_depth(session, s)
        if depth >= settings.reiterate_max_depth:
            log.info(
                "runner auto mode: strategy %s is at generation depth %d "
                "(reiterate_max_depth=%d); not queueing another iteration",
                sid,
                depth,
                settings.reiterate_max_depth,
            )
            return
        try:
            child_id = await reiterate(session, sid)
            log.info(
                "runner auto mode: iteration queued as strategy %s (parent %s)",
                child_id,
                sid,
            )
        except Exception:
            log.exception("runner auto mode: reiterate failed for strategy %s", sid)


def _dependent_pipeline_section(strategy_json: dict, dependent: str) -> str:
    """The inline-pipeline section that holds `dependent`, so the missing
    dependency is inserted into the same section (where `before` resolves).

    Falls back to "indicators" — the common case and the section a legacy
    top-level slug-list pipeline maps to."""
    pipeline = strategy_json.get("pipeline")
    if isinstance(pipeline, dict):
        for section in ("indicators", "preprocessing", "feature_selection"):
            entries = pipeline.get(section) or []
            if any(isinstance(e, dict) and e.get("name") == dependent for e in entries):
                return section
    return "indicators"


async def _repair_and_reiterate(session: AsyncSession, sid: int, err: RunnerConfigError) -> None:
    """Auto-repair a strategy whose backtest failed on a missing pipeline
    dependency by reiterating a child with the dependency inserted.

    fwbg reports `dependent` needs `dependency` upstream; we synthesize a
    deterministic modify_plugins recommendation that inserts `dependency`
    directly before `dependent`, reiterate a child (which the auto-runner
    backtests next), and abandon the broken parent so it is not re-backtested
    (which would spawn duplicate repair children). Bounded by reiterate_max_depth.
    """
    if not err.dependency or not err.dependent:
        return
    s = (await session.execute(select(Strategy).where(Strategy.id == sid))).scalar_one()
    depth = await generation_depth(session, s)
    if depth >= settings.reiterate_max_depth:
        log.info(
            "runner auto mode: strategy %s at generation depth %d (reiterate_max_depth=%d); "
            "not auto-repairing",
            sid,
            depth,
            settings.reiterate_max_depth,
        )
        return

    iteration_dir = strategy_dir(s.slug) / "iteration_001"
    iteration_dir.mkdir(parents=True, exist_ok=True)
    strategy_path = iteration_dir / "strategy.json"
    try:
        strategy_json = json.loads(strategy_path.read_text())
    except (OSError, json.JSONDecodeError):
        strategy_json = {}
    section = _dependent_pipeline_section(strategy_json, err.dependent)
    sidecar = {
        "kind": "modify_plugins",
        "ops": [
            {
                "action": "add",
                "section": section,
                "slug": err.dependency,
                "params": {},
                "before": err.dependent,
            }
        ],
        "confidence": 1.0,
        "reasoning": (
            f"Auto-repair: fwbg reported {err.dependent!r} depends on {err.dependency!r}, "
            f"which was missing from the pipeline. Inserting {err.dependency!r} before "
            f"{err.dependent!r}."
        ),
    }
    (iteration_dir / "analyst_recommendation.json").write_text(json.dumps(sidecar, indent=2))

    try:
        # repair=True: the parent is still PROPOSED (its backtest failed), so
        # bypass reiterate's BACKTESTED precondition for this corrective child.
        child_id = await reiterate(session, sid, repair=True)
    except Exception:
        log.exception("runner auto mode: auto-repair reiterate failed for strategy %s", sid)
        return

    pm_path = strategy_dir(s.slug) / "post_mortem.yaml"
    pm_path.parent.mkdir(parents=True, exist_ok=True)
    pm_path.write_text(
        yaml.safe_dump(
            {
                "slug": s.slug,
                "asset_class": s.asset_class,
                "strategy_family": s.strategy_family,
                "summary": (
                    f"Auto-repaired and superseded by child strategy {child_id}: fwbg "
                    f"reported {err.dependent!r} depends on {err.dependency!r}, which was "
                    f"missing from the pipeline. The child inserts it before "
                    f"{err.dependent!r}."
                ),
                "repair_child_id": child_id,
                "written_at": datetime.now(UTC).isoformat(),
            },
            sort_keys=False,
        )
    )
    try:
        await transition_strategy(
            session,
            s,
            StrategyState.ABANDONED,
            reason=(
                f"auto-repair: superseded by child strategy {child_id} "
                f"(inserted missing dependency {err.dependency!r} before {err.dependent!r})"
            ),
            payload={"repair_child_id": child_id, "post_mortem_path": str(pm_path)},
            created_by="auto_runner",
        )
    except Exception:
        log.exception("runner auto mode: could not abandon superseded parent %s", sid)

    log.info(
        "runner auto mode: auto-repaired strategy %s → child %s (inserted %s before %s)",
        sid,
        child_id,
        err.dependency,
        err.dependent,
    )


async def tick() -> int | None:
    """One auto-mode cycle. Priority: backtest a waiting PROPOSED strategy;
    if none, drain the backlog of backtested-but-unanalyzed strategies.

    Returns the strategy id that was worked on, or None if nothing ran. The
    heavy work is awaited here — the surrounding loop only polls again after
    it finished, which keeps the single-flight guarantee.
    """
    if not await is_enabled():
        return None

    # Housekeeping: capped PROPOSED strategies can never be auto-picked again;
    # abandon them so they stop clogging the pipeline-fill "active" count and
    # fresh research can start.
    async with SessionLocal() as session:
        await abandon_capped_proposed(session)

    async with SessionLocal() as session:
        sid = await pick_next_strategy_id(session)

    if sid is not None:
        log.info("runner auto mode: starting backtest for strategy %s", sid)
        backtest_ok = False
        config_error: RunnerConfigError | None = None
        async with SessionLocal() as session:
            s = (await session.execute(select(Strategy).where(Strategy.id == sid))).scalar_one()
            client = FwbgClient(base_url=settings.fwbg_api_url)
            try:
                await Runner(client, session).run(s)
                backtest_ok = True
            except RunnerConfigError as exc:
                # A fixable strategy-config error (e.g. a plugin missing its
                # pipeline dependency). Auto-repair it below instead of burning
                # retries on the same broken file until the cap abandons it.
                config_error = exc
                log.warning(
                    "runner auto mode: backtest for strategy %s hit a fixable config error: %s",
                    sid,
                    exc,
                )
            except Exception:
                # Runner marked its AgentRun failed; the attempt counter keeps a
                # persistently broken strategy from being retried forever.
                log.exception("runner auto mode: backtest failed for strategy %s", sid)
            finally:
                await client.aclose()

        if backtest_ok:
            async with SessionLocal() as session:
                await _analyze_and_apply(session, sid)
        elif config_error is not None:
            async with SessionLocal() as session:
                await _repair_and_reiterate(session, sid, config_error)
        return sid

    # Nothing to backtest → drain one backtested-but-unanalyzed strategy so
    # the backlog (e.g. from when the Analyst was crashing) advances instead
    # of sitting in BACKTESTED forever.
    async with SessionLocal() as session:
        pending = await pick_next_backtested_unanalyzed(session)
    if pending is not None:
        log.info("runner auto mode: analyzing backtested-but-unanalyzed strategy %s", pending)
        async with SessionLocal() as session:
            await _analyze_and_apply(session, pending)
        return pending

    # Nothing to analyze either → drive one pending add_indicator request
    # through the plugin chain (author → evaluate → reiterate-with-plugin)
    # so those strategies stop dead-ending in BACKTESTED.
    async with SessionLocal() as session:
        pending = await pick_next_add_indicator_pending(session)
    if pending is not None:
        log.info("runner auto mode: authoring requested plugin for strategy %s", pending)
        async with SessionLocal() as session:
            await _author_and_reiterate(session, pending)
        return pending

    # Last resort: retry translation for PROPOSED strategies that have a
    # hypothesis but no strategy.json (prior translator run failed). The LLM
    # is non-deterministic — a retry may produce a valid result.
    async with SessionLocal() as session:
        pending = await pick_next_untranslated_proposed(session)
    if pending is not None:
        log.info("runner auto mode: retrying translator for strategy %s", pending)
        async with SessionLocal() as session:
            await _retranslate(session, pending)
        return pending

    return None


async def run_loop() -> None:
    """Poll forever; meant to run as an asyncio background task."""
    log.info(
        "runner auto-mode loop started (poll=%ss, enabled=%s)",
        settings.runner_auto_poll_seconds,
        await is_enabled(),
    )
    while True:
        try:
            await tick()
        except Exception:
            log.exception("runner auto mode: tick failed")
        await asyncio.sleep(settings.runner_auto_poll_seconds)


async def _active_strategy_count(session: AsyncSession) -> int:
    """Count strategies that are genuinely in-flight: PROPOSED + BACKTESTED-unanalyzed.

    BACKTESTED strategies that already have a successful analyst run are NOT counted —
    they are done with their current iteration (exhausted reiterate chain,
    add_indicator waiting on plugin author, or ChangeExit with null new_exit_strategy).
    Counting them would permanently block the pipeline-fill trigger and prevent new
    research from starting.

    PROPOSED strategies without a strategy.json are also excluded: the runner
    will never pick them up, and they are either being retried by the
    retranslation path (tracked separately by _research_is_busy) or exhausted
    and due for abandonment. Counting them as active would starve fresh research.
    """
    proposed_rows = (
        (
            await session.execute(
                select(Strategy).where(Strategy.current_state == StrategyState.PROPOSED.value)
            )
        )
        .scalars()
        .all()
    )
    proposed = sum(
        1
        for s in proposed_rows
        if (strategy_dir(s.slug) / "iteration_001" / "strategy.json").is_file()
    )

    analyzed_ids = select(AgentRun.strategy_id).where(
        AgentRun.agent_name == "analyst",
        AgentRun.status == AgentRunStatus.DONE.value,
        AgentRun.strategy_id.isnot(None),
    )
    unanalyzed_backtested = (
        await session.execute(
            select(func.count())
            .select_from(Strategy)
            .where(
                Strategy.current_state == StrategyState.BACKTESTED.value,
                ~Strategy.id.in_(analyzed_ids),
            )
        )
    ).scalar_one()

    return int(proposed) + int(unanalyzed_backtested)


async def _research_is_busy(session: AsyncSession) -> bool:
    """Return True if a research_flow agent run is currently in-flight."""
    return bool(await _count_runs(session, "research_flow", statuses=_IN_FLIGHT_STATUSES))


async def _fill_pipeline_background(agent_run_id: int) -> None:
    """Run one asset-agnostic research cycle for pipeline auto-fill.

    Intentionally skips the auto-backtest step — the auto_runner picks up the
    resulting PROPOSED strategy on its next tick.
    """
    async with SessionLocal() as session:
        ar = (
            await session.execute(select(AgentRun).where(AgentRun.id == agent_run_id))
        ).scalar_one()
        ar.status = AgentRunStatus.RUNNING.value
        await session.commit()

        tavily = TavilyClient(api_key=get_secret("tavily"))
        brave = BraveClient(api_key=get_secret("brave"))
        search_client = FallbackSearchClient([tavily, brave])
        fwbg = FwbgClient(base_url=settings.fwbg_api_url)
        try:
            strategy_id = await research_and_translate(
                session,
                ResearcherInput(),
                search_client=search_client,
                fanout_n=settings.researcher_fanout_n,
                fwbg_client=fwbg,
            )
            await finish_agent_run(session, ar, status=AgentRunStatus.DONE, strategy_id=strategy_id)
            log.info("pipeline fill: research done, strategy %s now proposed", strategy_id)
        except Exception as exc:
            log.exception("pipeline fill: research failed (agent_run %s)", agent_run_id)
            await fail_agent_run(session, ar, exc)
        finally:
            await fwbg.aclose()
            await tavily.aclose()
            await brave.aclose()


async def pipeline_fill_loop() -> None:
    """Keep PROPOSED strategies at >= pipeline_min_proposed while runner-auto is on.

    Runs independently of run_loop so backtests (which can take hours) don't
    block pipeline refills. Fires at most one research run at a time.
    """
    log.info(
        "pipeline fill loop started (min=%d, poll=%ss)",
        await get_pipeline_min_proposed(),
        settings.pipeline_fill_poll_seconds,
    )
    while True:
        await asyncio.sleep(settings.pipeline_fill_poll_seconds)
        if not await is_enabled():
            continue
        try:
            async with SessionLocal() as session:
                if await _research_is_busy(session):
                    continue
                count = await _active_strategy_count(session)
                min_proposed = await get_pipeline_min_proposed()
                if count >= min_proposed:
                    continue
                log.info(
                    "pipeline fill: %d/%d active (proposed+backtested) — triggering research",
                    count,
                    min_proposed,
                )
                ar = await start_agent_run(
                    session,
                    agent_name="research_flow",
                    status=AgentRunStatus.PENDING,
                )
                task = asyncio.create_task(_fill_pipeline_background(ar.id))
                _background_tasks.add(task)
                task.add_done_callback(_background_tasks.discard)
        except Exception:
            log.exception("pipeline fill loop: tick failed")
