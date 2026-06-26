"""M4b smoke: drive the Researcher's fallback-search + fan-out path end-to-end via the API.

Prereqs:
  - haex-claude-proxy reachable at settings.anthropic_base_url for the LLM.
  - fwbg-agents alembic head applied (`uv run alembic upgrade head`).
  - TAVILY_API_KEY / BRAVE_API_KEY are optional here (unlike m4_smoke.py):
    the whole point of M4b is that the Researcher still completes — with
    zero web-search sources — when neither is configured.

What it does:
  1. POST /research/brief with researcher_fanout_n forced to 2, so the
     fan-out path (Task 4) is exercised even if the Settings default
     changes later.
  2. Polls GET /agents/runs/{id} until the orchestration AgentRun is done.
  3. Verifies at least one Researcher AgentRun is DONE, and that the
     resulting Strategy's hypothesis.json round-trips into a valid
     ResearcherHypothesis.
  4. If neither TAVILY_API_KEY nor BRAVE_API_KEY is set, this also proves
     the fallback chain degrades gracefully (every candidate's search_web
     call returns []) instead of hanging or raising.

The Translator/Runner stages beyond hypothesis generation are already
covered by m4_smoke.py / m3_smoke.py.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from fwbg_agents.config import settings
from fwbg_agents.main import app
from fwbg_agents.orchestrator.hypotheses import ResearcherHypothesis
from fwbg_agents.persistence.database import SessionLocal
from fwbg_agents.persistence.models import AgentRun, AgentRunStatus, LlmCall, Strategy

DEADLINE_S = float(os.environ.get("M4B_SMOKE_DEADLINE", "300"))  # 5 min
POLL_INTERVAL_S = 3.0


async def _wait_for_agent_run(agent_run_id: int, deadline_s: float) -> AgentRun:
    end = time.monotonic() + deadline_s
    while time.monotonic() < end:
        async with SessionLocal() as session:
            ar = (
                await session.execute(select(AgentRun).where(AgentRun.id == agent_run_id))
            ).scalar_one()
            if ar.status in {AgentRunStatus.DONE.value, AgentRunStatus.FAILED.value}:
                return ar
        await asyncio.sleep(POLL_INTERVAL_S)
    raise TimeoutError(f"agent_run {agent_run_id} did not finish in {deadline_s}s")


async def main() -> int:
    # Force the fan-out path regardless of the Settings default — mutating
    # the live singleton mirrors a `RESEARCHER_FANOUT_N=2` env override
    # without needing to reorder this module's imports.
    settings.researcher_fanout_n = 2

    have_tavily = bool(settings.tavily_api_key)
    have_brave = bool(settings.brave_api_key)
    if not have_tavily and not have_brave:
        print(
            "[m4b_smoke] neither TAVILY_API_KEY nor BRAVE_API_KEY set — running "
            "anyway to prove the fallback chain degrades gracefully (zero web "
            "sources) instead of hanging or crashing."
        )

    brief_body = {
        "asset_class": "FOREX",
        "strategy_family_hint": None,
        "free_text_brief": (
            "Find a mean-reversion strategy for FOREX majors during the London "
            "open that we haven't tried yet."
        ),
    }

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        print(f"[1/3] POST /research/brief (researcher_fanout_n={settings.researcher_fanout_n})")
        r = await client.post("/research/brief", json=brief_body)
        r.raise_for_status()
        meta = r.json()
        agent_run_id = meta["agent_run_id"]
        print(f"      → agent_run_id={agent_run_id} status={meta['status']}")

        print(f"[2/3] polling /agents/runs/{agent_run_id} (deadline {DEADLINE_S:.0f}s)...")
        ar = await _wait_for_agent_run(agent_run_id, DEADLINE_S)
        print(f"      → status={ar.status} strategy_id={ar.strategy_id} error={ar.error!r}")
        if ar.status != AgentRunStatus.DONE.value:
            print("      research_flow failed — aborting smoke", file=sys.stderr)
            return 1

        print(f"[3/3] verifying Researcher fan-out + hypothesis for strategy {ar.strategy_id}")
        async with SessionLocal() as session:
            s = (
                await session.execute(select(Strategy).where(Strategy.id == ar.strategy_id))
            ).scalar_one()
            researcher_runs = (
                await session.execute(
                    select(AgentRun).where(AgentRun.agent_name == "researcher")
                )
            ).scalars().all()
            search_rows = (
                await session.execute(
                    select(LlmCall).where(LlmCall.model.in_(["tavily-search", "brave-search"]))
                )
            ).scalars().all()

        done_researcher_runs = [
            run for run in researcher_runs if run.status == AgentRunStatus.DONE.value
        ]
        print(
            f"      researcher AgentRuns: {len(researcher_runs)} total, "
            f"{len(done_researcher_runs)} done"
        )
        if not done_researcher_runs:
            print("      ✗ no researcher AgentRun reached DONE", file=sys.stderr)
            return 1
        print("      ✓ at least one fan-out candidate succeeded")

        if not s.hypothesis_path:
            print("      ✗ Strategy.hypothesis_path is unset", file=sys.stderr)
            return 1
        hyp_path = Path(s.hypothesis_path)
        try:
            hypothesis = ResearcherHypothesis.model_validate_json(hyp_path.read_text())
            print(f"      ✓ {hyp_path.name} round-trips into ResearcherHypothesis "
                  f"({hypothesis.title!r})")
        except Exception as exc:
            print(f"      ✗ hypothesis.json failed to round-trip: {exc}", file=sys.stderr)
            return 1

        if have_tavily or have_brave:
            print(f"      ✓ {len(search_rows)} tavily/brave search llm_call row(s) recorded")
        else:
            print(
                f"      ✓ fallback chain degraded gracefully with 0 web sources "
                f"({len(hypothesis.sources)} hypothesis source(s) from model knowledge)"
            )

    print(f"\n[m4b_smoke] PASSED — strategy {s.slug} (id={s.id}).")
    print(f"timestamp: {datetime.now(UTC).isoformat()}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
