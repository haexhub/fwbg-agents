"""Per-run timeline events: append-only JSONL persistence + live SSE broadcast.

Every agent run owns a directory under ``settings.data_dir/agent-runs/<id>/``.
Timeline events are appended as JSON lines to ``events.jsonl`` *and* broadcast
on the in-memory SSE bus (:func:`fwbg_agents.events.emit`) so a live dashboard
sees them as they happen while a later-opened client can still replay the full
history from disk.

Design (Plan 006): "SQLite for metadata, filesystem for artifacts". Timeline
events are an append-only log with no cross-run query requirement, so a per-run
JSONL file — not a DB table — is the right home; no migration needed.

No ``asyncio`` lock is used: this is a single-process service and each
``emit_run_event`` appends exactly one line via a single ``write`` on a handle
opened in append mode, which the OS does not interleave with other appends to
the same file. Persistence errors are logged, never raised — an event must
never abort an agent run.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

from fwbg_agents import events as event_bus
from fwbg_agents.config import settings

log = logging.getLogger(__name__)

# Monotonic per-run sequence counters, seeded lazily from the on-disk line count
# on first access within this process (so restarts continue the sequence).
_seq_cache: dict[int, int] = {}


def run_dir(agent_run_id: int) -> Path:
    """Return the per-run artifact directory (may not exist yet)."""
    return settings.data_dir / "agent-runs" / str(agent_run_id)


def _events_file(agent_run_id: int) -> Path:
    return run_dir(agent_run_id) / "events.jsonl"


def _next_seq(agent_run_id: int) -> int:
    """Return the next monotonic sequence number for a run.

    Seeds the counter from the existing file's line count on first access so
    the sequence survives a process restart. Read failures fall back to 0.
    """
    if agent_run_id not in _seq_cache:
        count = 0
        path = _events_file(agent_run_id)
        try:
            if path.exists():
                with path.open("r", encoding="utf-8") as fh:
                    count = sum(1 for _ in fh)
        except OSError as exc:
            log.warning("run %s: could not seed seq from disk: %s", agent_run_id, exc)
        _seq_cache[agent_run_id] = count
    seq = _seq_cache[agent_run_id]
    _seq_cache[agent_run_id] = seq + 1
    return seq


def emit_run_event(
    agent_run_id: int, type: str, *, persist: bool = True, **payload: object
) -> None:
    """Append a timeline event to the run's JSONL log and broadcast it on SSE.

    ``type`` is the event kind (e.g. ``"research_search"``, ``"llm_tool_call"``).
    Extra keyword arguments become the event payload. Persistence failures are
    logged, never raised. The SSE broadcast always runs, even if the disk write
    failed, so a live dashboard is not starved by a transient disk error.

    ``persist=False`` makes the event live-only: it is broadcast on SSE (with a
    ``seq``/``ts`` like any other) but never written to ``events.jsonl``. Used
    for high-volume LLM token deltas (``llm_delta`` — Plan live-flow-overview
    WP-B3): persisting them would flood the log and the SSE queue drops at 200
    entries; a finished run replays its reasoning from the transcript instead.
    """
    event: dict = {
        "seq": _next_seq(agent_run_id),
        "ts": datetime.now(UTC).isoformat(),
        "type": type,
        **payload,
    }
    if persist:
        try:
            d = run_dir(agent_run_id)
            d.mkdir(parents=True, exist_ok=True)
            with _events_file(agent_run_id).open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, default=str) + "\n")
        except OSError as exc:
            log.warning("run %s: failed to persist event %s: %s", agent_run_id, type, exc)
    event_bus.emit({**event, "agent_run_id": agent_run_id})


def read_run_events(agent_run_id: int, limit: int = 500) -> list[dict]:
    """Return a run's persisted timeline events in sequence order.

    Returns the most recent ``limit`` events (an empty list for runs with no
    file — e.g. runs from before this feature). Truncating to the tail composes
    cleanly with live SSE backfill: the newest persisted events plus incoming
    live events form a gap-free timeline. Malformed lines are skipped.
    """
    path = _events_file(agent_run_id)
    if not path.exists():
        return []
    events: list[dict] = []
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError as exc:
        log.warning("run %s: failed to read events: %s", agent_run_id, exc)
    return events[-limit:]
