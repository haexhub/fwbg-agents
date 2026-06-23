"""M3 smoke: full pipeline against a live fwbg + a real LLM call.

Prereqs:
  - fwbg API up on http://localhost:8420 (FWBG_API_URL overrides).
  - haex-claude-proxy reachable at the URL in settings.anthropic_base_url.
  - fwbg-agents alembic head applied (`uv run alembic upgrade head`).

What it does:
  1. POST /strategies — seed a fresh strategy.
  2. POST /strategies/{id}/run — Runner copies strategy.json to fwbg's
     strategies dir, kicks off /api/runs/start, polls until terminal.
  3. POST /strategies/{id}/analyze — Analyst (LLM) emits a recommendation.
  4. Read back final state via /strategies/{id}.

Set MODE=kickoff to only schedule the run + analyze (useful when you don't
want to wait minutes for a full fwbg backtest). MODE=full (default) waits
for each phase to finish.
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

from fwbg_agents.main import app
from fwbg_agents.persistence.database import SessionLocal
from fwbg_agents.persistence.models import AgentRun, Strategy


MODE = os.environ.get("MODE", "full")  # full | kickoff
RUNNER_DEADLINE_S = float(os.environ.get("M3_SMOKE_RUNNER_TIMEOUT", "1800"))  # 30 min
ANALYST_DEADLINE_S = float(os.environ.get("M3_SMOKE_ANALYST_TIMEOUT", "120"))  # 2 min
POLL_INTERVAL_S = 3.0


async def _wait_for_agent_run(strategy_id: int, agent_name: str, deadline_s: float) -> AgentRun:
    end = time.monotonic() + deadline_s
    while time.monotonic() < end:
        async with SessionLocal() as session:
            ar = (
                await session.execute(
                    select(AgentRun)
                    .where(AgentRun.strategy_id == strategy_id, AgentRun.agent_name == agent_name)
                    .order_by(AgentRun.id.desc())
                )
            ).scalars().first()
            if ar and ar.status in {"done", "failed"}:
                return ar
        await asyncio.sleep(POLL_INTERVAL_S)
    raise TimeoutError(f"agent {agent_name} for strategy {strategy_id} did not finish in {deadline_s}s")


async def main() -> None:
    slug = f"m3_smoke_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}"
    # Use a minimal-but-valid fwbg strategy config. The 'deep_orb_index' preset
    # set in fwbg/strategies/configs is a known-good template.
    template_path = Path.home() / "fwbg" / "strategies" / "configs" / "deep_orb_index.json"
    if not template_path.is_file():
        print(f"ERROR: no template at {template_path}; cannot run smoke", file=sys.stderr)
        sys.exit(1)

    strategy_json = json.loads(template_path.read_text())
    strategy_json["name"] = slug  # fwbg uses 'name' as identity

    body = {
        "slug": slug,
        "asset_class": "INDEX",
        "strategy_family": "ORB",
        "strategy_json": strategy_json,
        "tags": ["m3-smoke", "orb"],
    }

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        print(f"[1/4] POST /strategies (slug={slug})")
        r = await client.post("/strategies", json=body)
        r.raise_for_status()
        meta = r.json()
        strategy_id = meta["id"]
        print(f"      → id={strategy_id} state={meta['current_state']} dir={meta['iteration_dir']}")

        print(f"[2/4] POST /strategies/{strategy_id}/run")
        r = await client.post(f"/strategies/{strategy_id}/run")
        r.raise_for_status()
        print(f"      → 202 scheduled, agent_run_id={r.json().get('agent_run_id')}")

        if MODE == "kickoff":
            print("MODE=kickoff: not waiting for completion")
            return

        print(f"      polling for Runner completion (deadline {RUNNER_DEADLINE_S:.0f}s)...")
        ar = await _wait_for_agent_run(strategy_id, "runner", RUNNER_DEADLINE_S)
        print(f"      → runner agent_run={ar.id} status={ar.status} output={ar.output_artifact_path!r}")
        if ar.status != "done":
            print(f"      runner failed; error={ar.error!r}", file=sys.stderr)
            return

        print(f"[3/4] POST /strategies/{strategy_id}/analyze (LLM)")
        r = await client.post(f"/strategies/{strategy_id}/analyze")
        r.raise_for_status()
        print(f"      → 202 scheduled")

        ar = await _wait_for_agent_run(strategy_id, "analyst", ANALYST_DEADLINE_S)
        print(f"      → analyst agent_run={ar.id} status={ar.status}")
        if ar.status == "done":
            report_path = Path(ar.output_artifact_path)
            if report_path.is_file():
                print(f"      analyst_report.md:")
                print("\n".join("        " + l for l in report_path.read_text().splitlines()))

        print(f"[4/4] GET /strategies/{strategy_id} (final state)")
        r = await client.get(f"/strategies/{strategy_id}")
        r.raise_for_status()
        body = r.json()
        st = body["strategy"]
        print(
            f"      → final state={st['current_state']} "
            f"transitions={len(body['transitions'])}"
        )
        for t in body["transitions"]:
            print(f"         {t['from_state']} → {t['to_state']} ({t['reason']!r})")


if __name__ == "__main__":
    asyncio.run(main())
