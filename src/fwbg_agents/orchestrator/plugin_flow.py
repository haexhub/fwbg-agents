"""Orchestration glue for plugin lifecycle endpoints.

M5d split-flow: `author_plugin_from_strategy` runs PluginPlanner →
PluginImplementer with two AgentRun rows ("plugin_planner" + "plugin_implementer")
and N LlmCall children for the implementer's refinement-loop. API envelope
unchanged (POST /strategies/{id}/author-plugin still returns the same shape).
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path

from pydantic_ai.models import Model
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from fwbg_agents.agents.plugin_evaluator import PluginEvaluator
from fwbg_agents.agents.plugin_implementer import (
    PluginImplementer,
    PluginImplementerError,
)
from fwbg_agents.agents.plugin_planner import (
    LlmCallMeta,
    PluginPlanner,
    PluginPlannerError,
)
from fwbg_agents.agents.translator import Translator
from fwbg_agents.config import settings
from fwbg_agents.orchestrator.lifecycle import strategy_dir, transition_plugin
from fwbg_agents.orchestrator.live_catalog import fetch_live_catalog
from fwbg_agents.orchestrator.plugin_contract import dump_contract
from fwbg_agents.persistence.agent_runs import fail_agent_run
from fwbg_agents.persistence.models import (
    AgentRun,
    AgentRunStatus,
    LlmCall,
    Plugin,
    PluginState,
    Strategy,
    StrategyState,
)
from fwbg_agents.tools.fwbg_client import FwbgClient

log = logging.getLogger(__name__)


class AuthorPluginPreconditionError(RuntimeError):
    """422 from POST /strategies/{id}/author-plugin."""


class PluginAuthorError(RuntimeError):
    """Wraps PluginPlannerError or PluginImplementerError for the API layer."""


class EvaluatePluginPreconditionError(RuntimeError):
    """422 from POST /plugins/{id}/evaluate."""


class ReiterateWithPluginPreconditionError(RuntimeError):
    """4xx from POST /strategies/{id}/reiterate-with-plugin (404 if the
    message starts with 'strategy ... not found', otherwise 422)."""


_ITERATION_RE = re.compile(r"^iteration_(\d+)$")


def _find_latest_sidecar(slug: str) -> Path | None:
    """Locate `add_indicator_request.json` from the latest iteration_NNN."""
    sdir = strategy_dir(slug)
    if not sdir.is_dir():
        return None
    candidates: list[tuple[int, Path]] = []
    for child in sdir.iterdir():
        if not child.is_dir():
            continue
        m = _ITERATION_RE.match(child.name)
        if not m:
            continue
        sidecar = child / "add_indicator_request.json"
        if sidecar.is_file():
            candidates.append((int(m.group(1)), sidecar))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1]


def _plugin_dir(slug: str) -> Path:
    """Return the filesystem directory for a plugin's generated artifacts."""
    return settings.data_dir / "plugins" / slug


async def _start_agent_run(
    session: AsyncSession,
    *,
    agent_name: str,
    strategy_id: int,
    input_artifact_path: str | None,
) -> AgentRun:
    """Create and persist a new AgentRun row in RUNNING state."""
    now = datetime.now(UTC)
    ar = AgentRun(
        agent_name=agent_name,
        status=AgentRunStatus.RUNNING.value,
        strategy_id=strategy_id,
        input_artifact_path=input_artifact_path,
        started_at=now,
        created_at=now,
    )
    session.add(ar)
    await session.commit()
    await session.refresh(ar)
    return ar


async def _finish_agent_run(
    session: AsyncSession,
    ar: AgentRun,
    *,
    status: AgentRunStatus,
    output_artifact_path: str | None = None,
    error: str | None = None,
    plugin_id: int | None = None,
) -> None:
    """Update an AgentRun row with final status, end time, and optional output fields."""
    ar.status = status.value
    ar.ended_at = datetime.now(UTC)
    if output_artifact_path is not None:
        ar.output_artifact_path = output_artifact_path
    if error is not None:
        ar.error = error
    if plugin_id is not None:
        ar.plugin_id = plugin_id
    await session.commit()


async def _persist_llm_call(
    session: AsyncSession,
    ar: AgentRun,
    meta: LlmCallMeta,
) -> None:
    """Persist an LlmCall row for token/latency accounting on the given agent run."""
    session.add(
        LlmCall(
            agent_run_id=ar.id,
            model=meta.model_name,
            input_tokens=meta.input_tokens,
            output_tokens=meta.output_tokens,
            latency_ms=meta.latency_ms,
            created_at=datetime.now(UTC),
        )
    )
    await session.commit()


async def author_plugin_from_strategy(
    session: AsyncSession,
    strategy_id: int,
    *,
    planner_model: Model | None = None,
    implementer_model: Model | None = None,
) -> int:
    """M5d: run PluginPlanner → PluginImplementer for a BACKTESTED strategy
    whose latest iteration has an add_indicator_request.json sidecar.

    Persists two AgentRun rows ("plugin_planner", "plugin_implementer") with N
    LlmCall children under the implementer-run for the refinement loop.

    Returns the new plugin.id on success; raises PluginAuthorError on
    planner or implementer failure (both AgentRuns are marked FAILED with
    appropriate error messages so the post-mortem trail is intact).
    """
    strategy = (
        await session.execute(select(Strategy).where(Strategy.id == strategy_id))
    ).scalar_one_or_none()
    if strategy is None:
        raise AuthorPluginPreconditionError(f"strategy {strategy_id} not found")

    if strategy.current_state != StrategyState.BACKTESTED.value:
        raise AuthorPluginPreconditionError(
            f"strategy {strategy.slug} is in state {strategy.current_state!r}; "
            "author-plugin requires BACKTESTED"
        )

    sidecar_path = _find_latest_sidecar(strategy.slug)
    if sidecar_path is None:
        raise AuthorPluginPreconditionError(
            f"no add_indicator_request.json found under data/strategies/{strategy.slug}/"
            f"iteration_NNN/; run /strategies/{strategy_id}/analyze first"
        )

    try:
        sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        # Corrupt sidecar is a deterministic failure: record a failed planner
        # run so the attempt consumes auto-retry budget and the strategy is not
        # re-picked on every tick (see pick_next_add_indicator_pending).
        planner_ar = await _start_agent_run(
            session,
            agent_name="plugin_planner",
            strategy_id=strategy.id,
            input_artifact_path=str(sidecar_path),
        )
        await fail_agent_run(session, planner_ar, exc)
        raise AuthorPluginPreconditionError(
            f"cannot parse sidecar at {sidecar_path}: {exc}"
        ) from exc
    except OSError as exc:
        # Transient I/O failure: don't charge the retry budget — the file may
        # be readable on the next tick.
        raise AuthorPluginPreconditionError(
            f"cannot read sidecar at {sidecar_path}: {exc}"
        ) from exc

    # fwbg is the single source of truth for the plugin catalog + example
    # source: fetch the live catalog and reuse the client for the planner's
    # in-tree examples. No filesystem fallback — a dead API fails loudly.
    client = FwbgClient(base_url=settings.fwbg_api_url)
    try:
        live = await fetch_live_catalog(session, client)

        # --- Phase 1: PluginPlanner -------------------------------------------
        planner_ar = await _start_agent_run(
            session,
            agent_name="plugin_planner",
            strategy_id=strategy.id,
            input_artifact_path=str(sidecar_path),
        )
        try:
            planner = PluginPlanner(model=planner_model)
            planner_result = await planner.run_plan(
                parent_strategy=strategy, sidecar=sidecar, live=live, client=client
            )
        except PluginPlannerError as exc:
            await fail_agent_run(session, planner_ar, exc)
            raise PluginAuthorError(f"planner failed: {exc}") from exc
        except Exception as exc:  # belt-and-suspenders for unexpected
            await fail_agent_run(session, planner_ar, exc)
            raise
    finally:
        await client.aclose()

    await _persist_llm_call(session, planner_ar, planner_result.llm)
    await _finish_agent_run(
        session,
        planner_ar,
        status=AgentRunStatus.DONE,
        output_artifact_path=str(planner_result.plan_path),
    )
    plan = planner_result.plan

    # --- Phase 2: PluginImplementer ------------------------------------------
    impl_ar = await _start_agent_run(
        session,
        agent_name="plugin_implementer",
        strategy_id=strategy.id,
        input_artifact_path=str(planner_result.plan_path),
    )
    try:
        implementer = PluginImplementer(model=implementer_model)
        impl_result = await implementer.run_implement(plan=plan)
    except PluginImplementerError as exc:
        for meta in exc.llm_calls:
            await _persist_llm_call(session, impl_ar, meta)
        # Stash last attempted code on disk for post-mortem.
        last_code_path: str | None = None
        if exc.last_code is not None:
            dir_ = settings.data_dir / "plugin-runs" / plan.slug
            dir_.mkdir(parents=True, exist_ok=True)
            last_code_path = str(dir_ / "last_failed_code.py")
            Path(last_code_path).write_text(exc.last_code, encoding="utf-8")
        await _finish_agent_run(
            session,
            impl_ar,
            status=AgentRunStatus.FAILED,
            error=exc.last_err or str(exc),
            output_artifact_path=last_code_path,
        )
        raise PluginAuthorError(f"implementer failed: {exc}") from exc
    except Exception as exc:
        await fail_agent_run(session, impl_ar, exc)
        raise

    for meta in impl_result.llm_calls:
        await _persist_llm_call(session, impl_ar, meta)

    output = impl_result.output

    # --- Phase 3: belt-and-suspenders slug-collision guard at DB level --------
    # Planner already checked the catalog, but a parallel author-session could
    # have landed the same slug between then and now.
    existing = (
        await session.execute(select(Plugin).where(Plugin.slug == output.slug))
    ).scalar_one_or_none()
    if existing is not None:
        await _finish_agent_run(
            session,
            impl_ar,
            status=AgentRunStatus.FAILED,
            error=f"slug {output.slug!r} already taken by plugin id={existing.id}",
        )
        raise PluginAuthorError(
            f"slug {output.slug!r} already exists as plugin id={existing.id}"
        )

    # --- Phase 4: persist artifacts + Plugin row + Transition ----------------
    target_dir = _plugin_dir(output.slug) / "v1"
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / "plugin.py").write_text(output.python_code, encoding="utf-8")
    dump_contract(output.contract, target_dir / "contract.yaml")
    (target_dir / "spec.md").write_text(output.spec_md, encoding="utf-8")

    now = datetime.now(UTC)
    plugin = Plugin(
        slug=output.slug,
        current_state=PluginState.SPECIFIED.value,
        kind=output.contract.kind,
        spec_path=str(target_dir / "spec.md"),
        contract_path=str(target_dir / "contract.yaml"),
        created_at=now,
        updated_at=now,
    )
    session.add(plugin)
    await session.flush()

    # Link both AgentRuns to the new plugin for traceability + capability lookup.
    planner_ar.plugin_id = plugin.id
    impl_ar.plugin_id = plugin.id

    await transition_plugin(
        session,
        plugin,
        PluginState.AUTHORED,
        reason="plugin_author",
        payload={
            "request_path": str(sidecar_path),
            "request_strategy_id": strategy.id,
            "rounds_used": impl_result.rounds_used,
            "planner_model": planner_result.llm.model_name,
            "implementer_model": impl_result.llm_calls[0].model_name
            if impl_result.llm_calls
            else "unknown",
        },
        created_by="plugin_author",
    )

    await _finish_agent_run(
        session,
        impl_ar,
        status=AgentRunStatus.DONE,
        output_artifact_path=str(target_dir / "contract.yaml"),
        plugin_id=plugin.id,
    )
    await session.refresh(plugin)
    return plugin.id


async def evaluate_plugin(session: AsyncSession, plugin_id: int) -> int:
    """Run PluginEvaluator for a plugin in AUTHORED state.
    Returns the verification_run_id.

    After a successful evaluation (VERIFIED), the plugin is registered with the
    fwbg backend so it appears immediately in GET /api/plugins without requiring
    a restart. Failures to register are logged but do not fail the evaluation —
    merge_with_db provides a fallback until Phase 3.3 removes it.
    """
    plugin = (
        await session.execute(select(Plugin).where(Plugin.id == plugin_id))
    ).scalar_one_or_none()
    if plugin is None:
        raise EvaluatePluginPreconditionError(f"plugin {plugin_id} not found")
    if plugin.current_state != PluginState.AUTHORED.value:
        raise EvaluatePluginPreconditionError(
            f"plugin {plugin.slug} is in state {plugin.current_state!r}; "
            "evaluate requires AUTHORED"
        )

    evaluator = PluginEvaluator(session)
    vr_id = await evaluator.run(plugin)

    await session.refresh(plugin)
    if plugin.current_state == PluginState.VERIFIED.value:
        await _register_verified_plugin_in_fwbg(plugin)

    return vr_id


async def _register_verified_plugin_in_fwbg(plugin: Plugin) -> None:
    """Ship a VERIFIED plugin to fwbg's registry via POST /api/plugins.

    Best-effort: logs a warning on failure rather than raising, so a transient
    fwbg outage does not roll back a correct evaluation result.
    """
    plugin_code_path = _plugin_dir(plugin.slug) / "v1" / "plugin.py"
    try:
        python_code = plugin_code_path.read_text(encoding="utf-8")
    except OSError as exc:
        log.warning(
            "register_plugin: cannot read plugin.py for %s at %s: %s",
            plugin.slug, plugin_code_path, exc,
        )
        return

    spec_md = ""
    if plugin.spec_path:
        with contextlib.suppress(OSError):
            spec_md = Path(plugin.spec_path).read_text(encoding="utf-8")

    client = FwbgClient(base_url=settings.fwbg_api_url)
    try:
        await client.register_plugin(
            slug=plugin.slug,
            python_code=python_code,
            kind=plugin.kind,
            spec_md=spec_md,
            overwrite=True,
        )
        log.info(
            "register_plugin: %s registered in fwbg as agent-authored:%s",
            plugin.slug, plugin.slug,
        )
    except Exception as exc:
        log.warning(
            "register_plugin: failed to register %s in fwbg (%s) — best-effort",
            plugin.slug, exc,
        )
    finally:
        await client.aclose()


async def reiterate_with_plugin(
    session: AsyncSession,
    strategy_id: int,
    plugin_slug: str,
) -> int:
    """Splice a VERIFIED plugin into a child Strategy via Translator.

    Preconditions (raise `ReiterateWithPluginPreconditionError`):
      1. parent Strategy exists ("not found" → 404 in API layer).
      2. parent.current_state == BACKTESTED.
      3. plugin (by slug) exists.
      4. plugin.current_state == VERIFIED.
      5. parent has a latest `add_indicator_request.json` sidecar.
      6. parent's sidecar `capability` matches the originating sidecar that
         was used to author this plugin (looked up via the plugin_planner
         AgentRun row for `plugin_id`).
    """
    parent = (
        await session.execute(select(Strategy).where(Strategy.id == strategy_id))
    ).scalar_one_or_none()
    if parent is None:
        raise ReiterateWithPluginPreconditionError(
            f"strategy {strategy_id} not found"
        )

    if parent.current_state != StrategyState.BACKTESTED.value:
        raise ReiterateWithPluginPreconditionError(
            f"strategy {parent.slug} is in state {parent.current_state!r}; "
            "reiterate-with-plugin requires BACKTESTED"
        )

    plugin = (
        await session.execute(select(Plugin).where(Plugin.slug == plugin_slug))
    ).scalar_one_or_none()
    if plugin is None:
        raise ReiterateWithPluginPreconditionError(
            f"plugin {plugin_slug!r} not found"
        )
    if plugin.current_state != PluginState.VERIFIED.value:
        raise ReiterateWithPluginPreconditionError(
            f"plugin {plugin.slug} is in state {plugin.current_state!r}; "
            "reiterate-with-plugin requires VERIFIED"
        )

    sidecar_path = _find_latest_sidecar(parent.slug)
    if sidecar_path is None:
        raise ReiterateWithPluginPreconditionError(
            f"no add_indicator_request.json found for {parent.slug}"
        )

    try:
        sidecar = json.loads(sidecar_path.read_text())
    except json.JSONDecodeError as exc:
        raise ReiterateWithPluginPreconditionError(
            f"cannot parse sidecar at {sidecar_path}: {exc}"
        ) from exc
    except OSError as exc:
        raise ReiterateWithPluginPreconditionError(
            f"cannot read sidecar at {sidecar_path}: {exc}"
        ) from exc

    parent_capability = sidecar.get("capability")
    plugin_capability = await lookup_plugin_capability(session, plugin.id)
    if plugin_capability is None or plugin_capability != parent_capability:
        raise ReiterateWithPluginPreconditionError(
            f"plugin {plugin.slug} capability={plugin_capability!r} does "
            f"not match sidecar capability={parent_capability!r}"
        )

    client = FwbgClient(base_url=settings.fwbg_api_url)
    try:
        translator = Translator(session, fwbg_client=client)
        child = await translator.run_reiterate_with_plugin(parent, plugin_slug, sidecar)
    finally:
        await client.aclose()
    return child.id


async def lookup_plugin_capability(
    session: AsyncSession, plugin_id: int
) -> str | None:
    """Read the originating sidecar's `capability` from the plugin_planner AR.

    The planner-run carries `input_artifact_path = str(sidecar_path)` and
    `plugin_id = plugin.id`. We pick the most recent DONE row.
    """
    ar = (
        await session.execute(
            select(AgentRun)
            .where(
                (AgentRun.plugin_id == plugin_id)
                & (AgentRun.agent_name == "plugin_planner")
                & (AgentRun.status == AgentRunStatus.DONE.value)
            )
            .order_by(desc(AgentRun.id))
            .limit(1)
        )
    ).scalar_one_or_none()
    if ar is None or not ar.input_artifact_path:
        return None
    try:
        data = json.loads(Path(ar.input_artifact_path).read_text())
    except (OSError, json.JSONDecodeError):
        return None
    cap = data.get("capability")
    return cap if isinstance(cap, str) else None


__all__ = [
    "AuthorPluginPreconditionError",
    "EvaluatePluginPreconditionError",
    "PluginAuthorError",
    "ReiterateWithPluginPreconditionError",
    "author_plugin_from_strategy",
    "evaluate_plugin",
    "lookup_plugin_capability",
    "reiterate_with_plugin",
]
