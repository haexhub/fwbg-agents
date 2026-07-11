"""M3 runs API — POST /strategies/{id}/run, /analyze, GET /agents/runs/{id}.

Both POST endpoints schedule a background task and return 202 immediately;
the background task creates a fresh DB session (the request session is closed
once the response is sent) and runs the agent. The endpoint pre-creates a
PENDING AgentRun row so callers can poll for status; the agent updates it
in-place to RUNNING / DONE / FAILED.

Tests monkeypatch `_run_runner_background` / `_run_analyst_background` to
avoid hitting fwbg or a real LLM.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import asc, desc, nulls_last, select
from sqlalchemy.ext.asyncio import AsyncSession

from fwbg_agents.agents.analyst import (
    Analyst,
    ChangeExit,
    ModifyPlugins,
    TuneParams,
    _best_symbol_metrics_from_results,
)
from fwbg_agents.agents.runner import Runner
from fwbg_agents.config import settings
from fwbg_agents.orchestrator import auto_runner
from fwbg_agents.orchestrator.lifecycle import strategy_dir
from fwbg_agents.orchestrator.recommendations import validate_and_apply
from fwbg_agents.orchestrator.research_flow import reiterate
from fwbg_agents.persistence.agent_runs import start_agent_run
from fwbg_agents.persistence.database import SessionLocal, get_session
from fwbg_agents.persistence.models import (
    AgentRun,
    AgentRunStatus,
    LlmCall,
    Strategy,
)
from fwbg_agents.run_events import read_run_events, run_dir
from fwbg_agents.tools.fwbg_client import FwbgClient

log = logging.getLogger(__name__)

router = APIRouter(tags=["runs"])


def _serialize_agent_run(ar: AgentRun) -> dict[str, Any]:
    """Serialize an AgentRun ORM row to a response dict."""
    return {
        "id": ar.id,
        "agent_name": ar.agent_name,
        "status": ar.status,
        "strategy_id": ar.strategy_id,
        "plugin_id": ar.plugin_id,
        "parent_run_id": ar.parent_run_id,
        "input_artifact_path": ar.input_artifact_path,
        "output_artifact_path": ar.output_artifact_path,
        "error": ar.error,
        "started_at": ar.started_at.isoformat() if ar.started_at else None,
        "ended_at": ar.ended_at.isoformat() if ar.ended_at else None,
    }


# ---------------------------------------------------------------------------
# Background-task entry points (monkeypatched in tests).
# ---------------------------------------------------------------------------


async def _run_runner_background(strategy_id: int) -> None:
    """Run the backtest runner task in the background."""
    async with SessionLocal() as session:
        s = (await session.execute(select(Strategy).where(Strategy.id == strategy_id))).scalar_one()
        client = FwbgClient(base_url=settings.fwbg_api_url)
        try:
            runner = Runner(client, session)
            await runner.run(s)
        except Exception:
            log.exception("runner background task failed for strategy %s", strategy_id)
        finally:
            await client.aclose()


async def _run_analyst_background(strategy_id: int) -> None:
    """Run the analyst task in the background."""
    async with SessionLocal() as session:
        s = (await session.execute(select(Strategy).where(Strategy.id == strategy_id))).scalar_one()
        client = FwbgClient(base_url=settings.fwbg_api_url)
        analyst = Analyst(session, fwbg_client=client)
        try:
            rec = await analyst.analyze(s)
            results_path = strategy_dir(s.slug) / "iteration_001" / "fwbg_results.json"
            metrics: dict[str, float] = {}
            if results_path.is_file():
                import json as _json
                results = _json.loads(results_path.read_text())
                metrics = {
                    k: float(v)
                    for k, v in _best_symbol_metrics_from_results(results).items()
                    if isinstance(v, (int, float))
                }
            try:
                await validate_and_apply(session, s, rec, metrics=metrics)
            except Exception as exc:
                log.warning("analyst recommendation rejected: %s", exc)
                return

            # TuneParams / ChangeExit / ModifyPlugins → queue a child PROPOSED
            # strategy for the auto-runner to pick up on the next free slot.
            # AddIndicator is picked up by the auto-runner's plugin-author chain.
            if isinstance(rec, (TuneParams, ChangeExit, ModifyPlugins)):
                try:
                    child_id = await reiterate(session, strategy_id)
                    log.info(
                        "analyst: iteration queued as strategy %s (parent %s)",
                        child_id, strategy_id,
                    )
                except Exception:
                    log.exception(
                        "analyst: reiterate failed for strategy %s", strategy_id
                    )
        except Exception:
            log.exception("analyst background task failed for strategy %s", strategy_id)
        finally:
            await client.aclose()


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/runner/auto")
async def get_runner_auto() -> dict[str, Any]:
    """Current state of the Runner auto mode (persisted flags)."""
    return {
        "enabled": auto_runner.is_enabled(),
        "pipeline_min_proposed": auto_runner.get_pipeline_min_proposed(),
    }


class RunnerAutoUpdate(BaseModel):
    """Payload for PUT /runner/auto — fields not supplied are left unchanged."""

    enabled: bool | None = None
    pipeline_min_proposed: int | None = None


@router.put("/runner/auto")
async def put_runner_auto(body: RunnerAutoUpdate) -> dict[str, Any]:
    """Update Runner auto mode settings. Any omitted field is left unchanged."""
    if body.enabled is not None:
        auto_runner.set_enabled(body.enabled)
    if body.pipeline_min_proposed is not None:
        auto_runner.set_pipeline_min_proposed(body.pipeline_min_proposed)
    return {
        "enabled": auto_runner.is_enabled(),
        "pipeline_min_proposed": auto_runner.get_pipeline_min_proposed(),
    }


@router.get("/runner/queue")
async def get_runner_queue(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """All PROPOSED strategies ordered by queue_position (nulls last), then created_at."""
    rows = (
        await session.execute(
            select(Strategy)
            .where(Strategy.current_state == "proposed")
            .order_by(nulls_last(Strategy.queue_position), Strategy.created_at)
        )
    ).scalars().all()
    return {
        "strategies": [
            {
                "id": s.id,
                "slug": s.slug,
                "strategy_family": s.strategy_family,
                "asset_class": s.asset_class,
                "queue_position": s.queue_position,
                "created_at": s.created_at.isoformat(),
            }
            for s in rows
        ]
    }


class QueueReorderBody(BaseModel):
    """Ordered list of strategy IDs for PUT /runner/queue."""

    order: list[int]


@router.put("/runner/queue")
async def put_runner_queue(
    body: QueueReorderBody,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Reorder the backtest queue.

    Accepts a list of strategy IDs. For each ID that belongs to a PROPOSED
    strategy, sets queue_position = 1-based index in the supplied list.
    IDs that are not PROPOSED are silently ignored.
    """
    if not body.order:
        return {"ok": True}

    proposed_ids = set(
        (
            await session.execute(
                select(Strategy.id).where(
                    Strategy.id.in_(body.order),
                    Strategy.current_state == "proposed",
                )
            )
        ).scalars().all()
    )

    position = 1
    for strategy_id in body.order:
        if strategy_id not in proposed_ids:
            continue
        s = (
            await session.execute(select(Strategy).where(Strategy.id == strategy_id))
        ).scalar_one()
        s.queue_position = position
        position += 1

    await session.commit()
    return {"ok": True}


@router.post("/strategies/{strategy_id}/run", status_code=202)
async def post_strategy_run(
    strategy_id: int,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Kick off a backtest run for a strategy. Returns a scheduled AgentRun."""
    s = (
        await session.execute(select(Strategy).where(Strategy.id == strategy_id))
    ).scalar_one_or_none()
    if s is None:
        raise HTTPException(404, f"strategy {strategy_id} not found")

    ar = await start_agent_run(
        session,
        agent_name="runner",
        strategy_id=strategy_id,
        status=AgentRunStatus.PENDING,
    )

    background_tasks.add_task(_run_runner_background, strategy_id)
    return {"strategy_id": strategy_id, "agent_run_id": ar.id, "status": "scheduled"}


@router.post("/strategies/{strategy_id}/analyze", status_code=202)
async def post_strategy_analyze(
    strategy_id: int,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Kick off the analyst against existing backtest results. Returns a scheduled AgentRun."""
    s = (
        await session.execute(select(Strategy).where(Strategy.id == strategy_id))
    ).scalar_one_or_none()
    if s is None:
        raise HTTPException(404, f"strategy {strategy_id} not found")

    results_path = strategy_dir(s.slug) / "iteration_001" / "fwbg_results.json"
    if not results_path.is_file():
        raise HTTPException(
            409,
            f"no fwbg results for {s.slug}; run /strategies/{strategy_id}/run first",
        )

    ar = await start_agent_run(
        session,
        agent_name="analyst",
        strategy_id=strategy_id,
        status=AgentRunStatus.PENDING,
    )

    background_tasks.add_task(_run_analyst_background, strategy_id)
    return {"strategy_id": strategy_id, "agent_run_id": ar.id, "status": "scheduled"}


@router.get("/agents/runs")
async def list_agent_runs(
    status: str | None = None,
    limit: int = 20,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """List agent runs, newest first. `status` is a comma-separated filter."""
    limit = max(1, min(limit, 100))
    q = select(AgentRun).order_by(desc(AgentRun.created_at)).limit(limit)
    if status:
        statuses = [s.strip() for s in status.split(",")]
        q = q.where(AgentRun.status.in_(statuses))
    rows = (await session.execute(q)).scalars().all()
    return {"runs": [_serialize_agent_run(r) for r in rows]}


_TRANSCRIPT_RE = re.compile(r"^transcript_(\d+)\.json$")
_ARTIFACT_SUFFIXES = frozenset({".json", ".md", ".py", ".txt"})
_ARTIFACT_MAX_BYTES = 512 * 1024


def _artifact_info(kind: str, path: str | None) -> dict[str, Any]:
    """Presence + size metadata for an input/output artifact path.

    Only paths that resolve under ``settings.data_dir`` are stat-ed; out-of-tree
    paths stored on the row report ``exists=False`` so the detail endpoint never
    leaks their existence/size (matches the ``/artifact`` content guard).
    """
    info: dict[str, Any] = {"kind": kind, "path": path, "exists": False, "size": None}
    if not path:
        return info
    p = Path(path).resolve()
    if not p.is_relative_to(settings.data_dir.resolve()):
        return info
    if p.is_file():
        info["exists"] = True
        info["size"] = p.stat().st_size
    return info


def _list_transcripts(agent_run_id: int) -> list[dict[str, Any]]:
    """Round number + size for each transcript_NNN.json on disk, ascending."""
    d = run_dir(agent_run_id)
    if not d.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for f in d.iterdir():
        m = _TRANSCRIPT_RE.match(f.name)
        if m and f.is_file():
            out.append({"round": int(m.group(1)), "size": f.stat().st_size})
    return sorted(out, key=lambda t: t["round"])


@router.get("/agents/runs/{agent_run_id}")
async def get_agent_run(
    agent_run_id: int, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    """Retrieve a single agent run by ID, enriched with LLM-call telemetry,
    transcript-round index, and artifact metadata (additive to the flat row)."""
    ar = (
        await session.execute(select(AgentRun).where(AgentRun.id == agent_run_id))
    ).scalar_one_or_none()
    if ar is None:
        raise HTTPException(404, f"agent_run {agent_run_id} not found")

    calls = (
        await session.execute(
            select(LlmCall)
            .where(LlmCall.agent_run_id == agent_run_id)
            .order_by(asc(LlmCall.created_at))
        )
    ).scalars().all()
    llm_calls = [
        {
            "model": c.model,
            "input_tokens": c.input_tokens,
            "output_tokens": c.output_tokens,
            "latency_ms": c.latency_ms,
            "created_at": c.created_at.isoformat() if c.created_at else None,
        }
        for c in calls
    ]

    # Flow drill-down (Plan 008 Schritt 5): child runs spawned under this run.
    children = (
        await session.execute(
            select(AgentRun)
            .where(AgentRun.parent_run_id == agent_run_id)
            .order_by(asc(AgentRun.started_at))
        )
    ).scalars().all()

    body = _serialize_agent_run(ar)
    body["llm_calls"] = llm_calls
    body["total_input_tokens"] = sum(c.input_tokens for c in calls)
    body["total_output_tokens"] = sum(c.output_tokens for c in calls)
    body["children"] = [
        {"id": c.id, "agent_name": c.agent_name, "status": c.status} for c in children
    ]
    body["transcripts"] = _list_transcripts(agent_run_id)
    body["artifacts"] = [
        _artifact_info("input", ar.input_artifact_path),
        _artifact_info("output", ar.output_artifact_path),
    ]
    return body


@router.get("/agents/runs/{agent_run_id}/transcript")
async def get_agent_run_transcript(
    agent_run_id: int,
    round: int = 1,
    session: AsyncSession = Depends(get_session),
) -> Any:
    """Return the parsed pydantic-ai message transcript for one LLM round.

    404 if the run or the requested round's transcript file is absent.
    """
    ar = (
        await session.execute(select(AgentRun).where(AgentRun.id == agent_run_id))
    ).scalar_one_or_none()
    if ar is None:
        raise HTTPException(404, f"agent_run {agent_run_id} not found")
    path = run_dir(agent_run_id) / f"transcript_{round:03d}.json"
    if not path.is_file():
        raise HTTPException(404, f"no transcript for run {agent_run_id} round {round}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(500, f"transcript unreadable: {exc}") from exc


@router.get("/agents/runs/{agent_run_id}/artifact")
async def get_agent_run_artifact(
    agent_run_id: int,
    kind: str = "output",
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Return the text content of a run's input/output artifact.

    Security: the resolved real path MUST live under ``settings.data_dir`` (403
    on traversal), the suffix must be a known text type, and the payload is
    capped at 512 KB. Untrusted content — the dashboard renders it as text only.
    """
    if kind not in ("input", "output"):
        raise HTTPException(422, "kind must be 'input' or 'output'")
    ar = (
        await session.execute(select(AgentRun).where(AgentRun.id == agent_run_id))
    ).scalar_one_or_none()
    if ar is None:
        raise HTTPException(404, f"agent_run {agent_run_id} not found")

    raw = ar.input_artifact_path if kind == "input" else ar.output_artifact_path
    if not raw:
        raise HTTPException(404, f"run {agent_run_id} has no {kind} artifact")

    path = Path(raw).resolve()
    data_root = settings.data_dir.resolve()
    if not path.is_relative_to(data_root):
        raise HTTPException(403, "artifact path escapes the data directory")
    if path.suffix not in _ARTIFACT_SUFFIXES:
        raise HTTPException(415, f"unsupported artifact type {path.suffix!r}")
    if not path.is_file():
        raise HTTPException(404, f"{kind} artifact file not found")
    size = path.stat().st_size
    # Read at most the cap in bytes (never the whole file), then decode — so a
    # huge artifact can't blow up memory and `truncated` is byte-accurate even
    # for multibyte UTF-8 (char-slicing the cap could exceed the byte budget).
    try:
        with path.open("rb") as fh:
            head = fh.read(_ARTIFACT_MAX_BYTES + 1)
    except OSError as exc:
        raise HTTPException(500, f"artifact unreadable: {exc}") from exc
    truncated = len(head) > _ARTIFACT_MAX_BYTES
    content = head[:_ARTIFACT_MAX_BYTES].decode("utf-8", errors="replace")
    return {
        "kind": kind,
        "path": str(path),
        "suffix": path.suffix,
        "size": size,
        "truncated": truncated,
        "content": content,
    }


@router.get("/agents/runs/{agent_run_id}/events")
async def get_agent_run_events(
    agent_run_id: int, session: AsyncSession = Depends(get_session)
) -> list[dict[str, Any]]:
    """Return a run's persisted timeline events in sequence order (bare array).

    404 if the run does not exist; an empty list is valid (runs from before the
    timeline feature have no events file). Matches the dashboard proxy contract
    `server/api/agents/runs/[id]/events.get.ts` (Array<Record<string, unknown>>).
    """
    ar = (
        await session.execute(select(AgentRun).where(AgentRun.id == agent_run_id))
    ).scalar_one_or_none()
    if ar is None:
        raise HTTPException(404, f"agent_run {agent_run_id} not found")
    return read_run_events(agent_run_id)
