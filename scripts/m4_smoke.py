"""M4 smoke: drive Researcher + Translator end-to-end via the API.

Prereqs:
  - TAVILY_API_KEY set (script exits cleanly otherwise — tests are mocked,
    but the smoke needs real web search to validate quota tracking).
  - haex-claude-proxy reachable at settings.anthropic_base_url for the LLM.
  - fwbg-agents alembic head applied (`uv run alembic upgrade head`).

What it does:
  1. POST /research/brief — Researcher emits a hypothesis, Translator
     converts it into a fwbg strategy.json. All artifacts land under
     data/strategies/<slug>/iteration_001/.
  2. Polls GET /agents/runs/{id} until the orchestration AgentRun is done.
  3. Verifies hypothesis.json + research_notes.md + strategy.json + spec.md
     are all on disk, validate_strategy_json passes, and at least one
     llm_call row with model='tavily-search' was recorded.

The Runner is NOT kicked off here — that's already covered by m3_smoke.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from fwbg_agents.config import settings
from fwbg_agents.main import app
from fwbg_agents.orchestrator.strategy_validator import (
    StrategyValidationError,
    validate_strategy_json,
)
from fwbg_agents.persistence.database import SessionLocal
from fwbg_agents.persistence.models import AgentRun, LlmCall, Strategy

DEADLINE_S = float(os.environ.get("M4_SMOKE_DEADLINE", "300"))  # 5 min
POLL_INTERVAL_S = 3.0


async def _wait_for_agent_run(agent_run_id: int, deadline_s: float) -> AgentRun:
    end = time.monotonic() + deadline_s
    while time.monotonic() < end:
        async with SessionLocal() as session:
            ar = (
                await session.execute(select(AgentRun).where(AgentRun.id == agent_run_id))
            ).scalar_one()
            if ar.status in {"done", "failed"}:
                return ar
        await asyncio.sleep(POLL_INTERVAL_S)
    raise TimeoutError(f"agent_run {agent_run_id} did not finish in {deadline_s}s")


async def main() -> int:
    if not settings.tavily_api_key:
        print(
            "TAVILY_API_KEY is unset — skipping M4 smoke. The Researcher+Translator\n"
            "tests are mocked and pass without it; this script needs a real key to\n"
            "validate Tavily quota tracking end-to-end.",
            file=sys.stderr,
        )
        return 0

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
        print("[1/3] POST /research/brief")
        r = await client.post("/research/brief", json=brief_body)
        r.raise_for_status()
        meta = r.json()
        agent_run_id = meta["agent_run_id"]
        print(f"      → agent_run_id={agent_run_id} status={meta['status']}")

        print(f"[2/3] polling /agents/runs/{agent_run_id} (deadline {DEADLINE_S:.0f}s)...")
        ar = await _wait_for_agent_run(agent_run_id, DEADLINE_S)
        print(f"      → status={ar.status} strategy_id={ar.strategy_id} error={ar.error!r}")
        if ar.status != "done":
            print("      research_flow failed — aborting smoke", file=sys.stderr)
            return 1

        print(f"[3/3] verifying artifacts for strategy {ar.strategy_id}")
        async with SessionLocal() as session:
            s = (
                await session.execute(select(Strategy).where(Strategy.id == ar.strategy_id))
            ).scalar_one()
            tavily_rows = (
                (await session.execute(select(LlmCall).where(LlmCall.model == "tavily-search")))
                .scalars()
                .all()
            )

        it_dir = Path(s.hypothesis_path).parent
        artifacts = {
            "hypothesis.json": it_dir / "hypothesis.json",
            "research_notes.md": it_dir / "research_notes.md",
            "strategy.json": it_dir / "strategy.json",
            "spec.md": it_dir / "spec.md",
        }
        for name, path in artifacts.items():
            ok = path.is_file()
            print(f"      {'✓' if ok else '✗'} {name}: {path}")
            if not ok:
                print(f"      missing artifact: {path}", file=sys.stderr)
                return 1

        try:
            validate_strategy_json(json.loads(artifacts["strategy.json"].read_text()))
            print("      ✓ validate_strategy_json passed")
        except StrategyValidationError as exc:
            print(f"      ✗ validate_strategy_json failed: {exc}", file=sys.stderr)
            return 1

        if not tavily_rows:
            print(
                "      ⚠ no llm_call row with model='tavily-search' — Researcher "
                "may not have used web search this round.",
            )
        else:
            print(f"      ✓ {len(tavily_rows)} tavily-search llm_call row(s) recorded")

    print(f"\nM4 smoke complete: strategy {s.slug} (id={s.id}) ready for Runner.")
    print(f"timestamp: {datetime.now(UTC).isoformat()}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
