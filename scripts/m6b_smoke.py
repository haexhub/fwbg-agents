"""M6b smoke: end-to-end paper-analyst + promote-live via the API.

Walks the full M6b happy path:

    [seed]                                Strategy in PAPER_TRADING +
                                          synthesised account-trades fixtures
                                          that clear the forex paper-criteria
    POST /strategies/{id}/paper-analyze   → 202 + agent_run_id
    [poll]                                AgentRun → DONE
    [sidecar]                             paper_analyst_<ar_id>.json with
                                          decision=promote_paper_to_live
    [metadata]                            paper_analyst_promote_recommended=True
    POST /strategies/{id}/promote-live    → 200 + new_state=live_trading
    [final]                               Strategy.current_state=LIVE_TRADING,
                                          metadata flag cleared, promoted_live_at
                                          stamped, Transition row exists,
                                          AgentRun(promote_live, DONE) exists

LLM mocking: the smoke monkey-patches `paper_flow.PaperAnalyst` to a stub
that returns a fixed `PromotePaperToLive`. The ASGITransport keeps the
background task in-process, so the patch reaches the call site.

The smoke owns its own subdirectory of `data/` for fixtures so it does not
collide with real fwbg `data/account-trades/` content. Stage-0 cleanup wipes
prior smoke artifacts (Strategy + AgentRuns + Transitions + on-disk dirs)
so re-runs work without manual intervention.

Prereq: `uv run alembic upgrade head`.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from fwbg_agents.config import settings
from fwbg_agents.main import app
from fwbg_agents.orchestrator import paper_flow as _paper_flow
from fwbg_agents.orchestrator.lifecycle import strategy_dir
from fwbg_agents.persistence.database import SessionLocal
from fwbg_agents.persistence.models import (
    AgentRun,
    AgentRunStatus,
    EntityType,
    Strategy,
    StrategyState,
    Transition,
)

SMOKE_STRATEGY_SLUG = "paper-smoke-test-001"
SMOKE_PAPER_PHASE_TARGET_DAYS = 90
SMOKE_ASSET_CLASS = "forex"

# Smoke-local fwbg_data_dir so we don't pollute the real fwbg data tree.
SMOKE_FWBG_DATA_DIR = Path("data/smoke/m6b")

DEADLINE_S = 30.0
POLL_INTERVAL_S = 0.25


# ---------------------------------------------------------------------------
# LLM stub — monkey-patches paper_flow.PaperAnalyst so paper_analyze() never
# instantiates the real LLM-backed analyst. Same-process ASGITransport keeps
# the BackgroundTask in this interpreter, so the patch reaches the call site.
# ---------------------------------------------------------------------------


class _SmokeAnalyst:
    """Stand-in for PaperAnalyst that always recommends promote-to-live."""

    def analyze_sync(self, **kwargs):
        from fwbg_agents.agents.paper_analyst import PromotePaperToLive

        return PromotePaperToLive(
            rationale="smoke: forex paper-criteria satisfied, promote",
        )


def _patch_paper_analyst_stub() -> None:
    _paper_flow.PaperAnalyst = _SmokeAnalyst  # type: ignore[misc,assignment]


# ---------------------------------------------------------------------------
# Fixture synthesis
# ---------------------------------------------------------------------------


def _write_fixture_files(data_dir: Path, slug: str) -> None:
    """Write trades.jsonl (50), status.json, positions.json.

    Tuned so `evaluate_paper_criteria` against forex.yaml passes:
      - sharpe_paper ≥ 1.0
      - win_rate ≥ 0.45
      - trades_total ≥ 30
      - max_dd_paper ≤ 0.15

    Trade pnls are spread across multiple magnitudes (not just two values)
    so `pstdev` is non-trivial and sharpe lands in a sensible 1.0..2.5
    range rather than blowing up to single-digit territory.
    """
    acct_dir = data_dir / "account-trades" / slug
    acct_dir.mkdir(parents=True, exist_ok=True)

    n = 50
    # 28 winners, 22 losers → win_rate = 0.56 (≥ 0.45). Spread pnl magnitudes
    # across each side so pstdev > 0 but not too small.
    # Winners (28): mix of 0.004/0.006/0.008/0.010 — mean ≈ 0.007
    # Losers  (22): mix of -0.003/-0.004/-0.005/-0.006 — mean ≈ -0.0045
    win_pnls = [0.004, 0.006, 0.008, 0.010] * 7  # 28 values
    loss_pnls = [-0.003, -0.004, -0.005, -0.006] * 5 + [-0.003, -0.005]  # 22 values
    assert len(win_pnls) == 28 and len(loss_pnls) == 22

    # Interleave wins/losses across the 45-day span so entry_times are well
    # distributed. Order alternates roughly 1 loss every ~2.3 wins.
    pnls: list[float] = []
    wi = li = 0
    for i in range(n):
        # Pattern: L W W L W W L W W ... → 33/17 split, too skewed.
        # Use modular cadence to land on 28 wins / 22 losses precisely.
        is_loser = ((i * 22) % n) < 22 and li < 22
        if is_loser:
            pnls.append(loss_pnls[li])
            li += 1
        else:
            pnls.append(win_pnls[wi])
            wi += 1
    # If the cadence didn't exactly hit 28/22, refill the tail.
    while wi < 28:
        pnls.append(win_pnls[wi])
        wi += 1
    while li < 22:
        pnls.append(loss_pnls[li])
        li += 1
    pnls = pnls[:n]

    now = datetime.now(UTC)
    first_entry = now - timedelta(days=45)
    last_entry = now - timedelta(hours=1)
    delta = (last_entry - first_entry) / (n - 1)

    trades_lines: list[str] = []
    equity = 10000.0
    starting_equity = equity
    curve: list[dict] = [
        {"timestamp": first_entry.isoformat(), "equity": round(equity, 2)}
    ]
    for i, pnl in enumerate(pnls):
        entry_time = first_entry + delta * i
        side = "buy" if i % 2 == 0 else "sell"
        entry_price = 1.08
        exit_price = entry_price * (1 + pnl)
        trade = {
            "symbol": "EURUSD",
            "side": side,
            "quantity": 1000,
            "entry_time": entry_time.isoformat(),
            "exit_time": (entry_time + timedelta(hours=2)).isoformat(),
            "entry_price": entry_price,
            "exit_price": exit_price,
            "pnl_pct": pnl,
        }
        trades_lines.append(json.dumps(trade))
        # Equity moves by ~pnl * quantity at 1.0 leverage (approximation —
        # close enough to drive max_dd computation).
        equity *= 1 + pnl
        curve.append(
            {
                "timestamp": (entry_time + timedelta(hours=2)).isoformat(),
                "equity": round(equity, 2),
            }
        )
    (acct_dir / "trades.jsonl").write_text("\n".join(trades_lines) + "\n")

    status = {
        "current_equity": round(equity, 2),
        "starting_equity": starting_equity,
        "sharpe_paper": 1.2,
        "max_dd_paper": 0.10,
        "trades_total": n,
        "days_in_paper": 45,
        "win_rate": 0.56,
        "equity_curve_sample": curve,
    }
    (acct_dir / "status.json").write_text(json.dumps(status, indent=2))

    positions = {
        "strategy_slug": slug,
        "updated_at": now.isoformat(),
        "positions": [
            {
                "symbol": "EURUSD",
                "side": "buy",
                "quantity": 1000,
                "entry_price": 1.08,
                "current_price": 1.085,
                "stop_loss": 1.07,
                "take_profit": 1.10,
                "unrealised_pnl_pct": 0.0046,
                "opened_at": (now - timedelta(hours=3)).isoformat(),
            },
        ],
    }
    (acct_dir / "positions.json").write_text(json.dumps(positions, indent=2))


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


async def _seed_strategy() -> int:
    """Insert (or refresh) a PAPER_TRADING Strategy."""
    now = datetime.now(UTC)
    async with SessionLocal() as session:
        existing = (
            await session.execute(
                select(Strategy).where(Strategy.slug == SMOKE_STRATEGY_SLUG)
            )
        ).scalar_one_or_none()
        if existing is not None:
            existing.current_state = StrategyState.PAPER_TRADING.value
            existing.asset_class = SMOKE_ASSET_CLASS
            existing.paper_phase_target_days = SMOKE_PAPER_PHASE_TARGET_DAYS
            existing.metadata_json = {}
            existing.updated_at = now
            await session.commit()
            return existing.id

        s = Strategy(
            slug=SMOKE_STRATEGY_SLUG,
            current_state=StrategyState.PAPER_TRADING.value,
            iteration_count=0,
            asset_class=SMOKE_ASSET_CLASS,
            strategy_family="ORB",
            paper_phase_target_days=SMOKE_PAPER_PHASE_TARGET_DAYS,
            metadata_json={},
            created_at=now,
            updated_at=now,
        )
        session.add(s)
        await session.commit()
        await session.refresh(s)
        return s.id


async def _cleanup_previous_run() -> None:
    """Stage-0 idempotency: wipe prior smoke artifacts.

    Removes (best-effort, no-op when absent):
      - DB AgentRun rows pointing at the prior Strategy (FK has no cascade)
      - DB Transition rows for entity_type=STRATEGY, entity_id=prior
      - DB Strategy row
      - On-disk <fwbg_data_dir>/account-trades/<slug>/ (telemetry fixtures)
      - On-disk <data_dir>/strategies/<slug>/ (sidecars)
    """
    async with SessionLocal() as session:
        prior = (
            await session.execute(
                select(Strategy).where(Strategy.slug == SMOKE_STRATEGY_SLUG)
            )
        ).scalar_one_or_none()
        if prior is not None:
            await session.execute(
                delete(AgentRun).where(AgentRun.strategy_id == prior.id)
            )
            await session.execute(
                delete(Transition).where(
                    Transition.entity_type == EntityType.STRATEGY.value,
                    Transition.entity_id == prior.id,
                )
            )
            await session.delete(prior)
            await session.commit()

    acct_dir = SMOKE_FWBG_DATA_DIR / "account-trades" / SMOKE_STRATEGY_SLUG
    if acct_dir.exists():
        shutil.rmtree(acct_dir)
    s_dir = strategy_dir(SMOKE_STRATEGY_SLUG)
    if s_dir.exists():
        shutil.rmtree(s_dir)


async def _wait_for_run(agent_run_id: int, deadline_s: float = DEADLINE_S) -> AgentRun:
    end = time.monotonic() + deadline_s
    while time.monotonic() < end:
        async with SessionLocal() as session:
            ar = (
                await session.execute(
                    select(AgentRun).where(AgentRun.id == agent_run_id)
                )
            ).scalar_one()
            if ar.status in {AgentRunStatus.DONE.value, AgentRunStatus.FAILED.value}:
                return ar
        await asyncio.sleep(POLL_INTERVAL_S)
    raise TimeoutError(f"agent_run {agent_run_id} did not finish in {deadline_s}s")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main() -> int:
    # Override fwbg_data_dir so the endpoints + fixture writer agree on the
    # same root. The override sticks for the lifetime of the process.
    settings.fwbg_data_dir = SMOKE_FWBG_DATA_DIR.resolve()
    print(f"[m6b_smoke] data_dir={settings.data_dir} fwbg_data_dir={settings.fwbg_data_dir}")

    print("[m6b_smoke] [0/10] cleanup of prior smoke artifacts (idempotent)")
    await _cleanup_previous_run()

    print("[m6b_smoke] [1/10] patch PaperAnalyst → _SmokeAnalyst (no real LLM)")
    _patch_paper_analyst_stub()

    print(
        f"[m6b_smoke] [2/10] seed Strategy in PAPER_TRADING "
        f"(slug={SMOKE_STRATEGY_SLUG!r} asset_class={SMOKE_ASSET_CLASS!r} "
        f"paper_phase_target_days={SMOKE_PAPER_PHASE_TARGET_DAYS})"
    )
    strategy_id = await _seed_strategy()
    print(f"       → strategy_id={strategy_id}")

    print(
        "[m6b_smoke] [3/10] synthesise telemetry: trades.jsonl (50 trades, 45-day span), "
        "status.json (equity climb w/ dip), positions.json (1 EURUSD position w/ SL/TP)"
    )
    _write_fixture_files(settings.fwbg_data_dir, SMOKE_STRATEGY_SLUG)

    # Pre-flight: run evaluate_paper_criteria against the synthesised summary
    # so we catch a tuning-regression before kicking the API.
    from fwbg_agents.orchestrator.criteria_paper import (
        evaluate_paper_criteria,
        load_paper_criteria,
    )
    from fwbg_agents.tools.fwbg_paper_reader import read_paper_summary

    summary = read_paper_summary(SMOKE_STRATEGY_SLUG, settings.fwbg_data_dir)
    assert summary is not None
    criteria = load_paper_criteria(SMOKE_ASSET_CLASS)
    eval_res = evaluate_paper_criteria(summary, criteria)
    print(
        f"       → summary: sharpe_paper={summary.sharpe_paper:.3f} "
        f"win_rate={summary.win_rate:.3f} trades_total={summary.trades_total} "
        f"max_dd_paper={summary.max_dd_paper:.3f} days_in_paper={summary.days_in_paper}"
    )
    if not eval_res.passed:
        print(
            f"       ✗ criteria failed; smoke cannot proceed: {eval_res.failures}",
            file=sys.stderr,
        )
        return 1
    print("       ✓ criteria passed — synthesised data clears forex.yaml gates")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        print("[m6b_smoke] [4/10] POST /strategies/{id}/paper-analyze")
        r = await client.post(f"/strategies/{strategy_id}/paper-analyze")
        if r.status_code != 202:
            print(f"       ✗ unexpected status {r.status_code}: {r.text}", file=sys.stderr)
            return 1
        ar_id = r.json()["agent_run_id"]
        print(f"       ✓ scheduled agent_run_id={ar_id}")

        print(f"[m6b_smoke] [5/10] poll AgentRun {ar_id} until DONE/FAILED (≤{DEADLINE_S}s)")
        ar = await _wait_for_run(ar_id)
        if ar.status != AgentRunStatus.DONE.value:
            print(
                f"       ✗ paper-analyze failed: status={ar.status} error={ar.error!r}",
                file=sys.stderr,
            )
            return 1
        print(f"       ✓ status=DONE sidecar={ar.output_artifact_path}")

        print("[m6b_smoke] [6/10] assert sidecar exists + decision=promote_paper_to_live")
        sidecar_path = strategy_dir(SMOKE_STRATEGY_SLUG) / f"paper_analyst_{ar_id}.json"
        if not sidecar_path.is_file():
            print(f"       ✗ sidecar missing: {sidecar_path}", file=sys.stderr)
            return 1
        sidecar_payload = json.loads(sidecar_path.read_text())
        if sidecar_payload.get("decision") != "promote_paper_to_live":
            print(
                f"       ✗ unexpected decision: {sidecar_payload.get('decision')!r}",
                file=sys.stderr,
            )
            return 1
        print("       ✓ sidecar.decision=promote_paper_to_live")

        print("[m6b_smoke] [7/10] refresh Strategy + assert paper_analyst_promote_recommended=True")
        async with SessionLocal() as session:
            s = (
                await session.execute(
                    select(Strategy).where(Strategy.id == strategy_id)
                )
            ).scalar_one()
            meta_after_analyze = dict(s.metadata_json or {})
        if not meta_after_analyze.get("paper_analyst_promote_recommended"):
            print(
                f"       ✗ flag not set: metadata_json={meta_after_analyze!r}",
                file=sys.stderr,
            )
            return 1
        print("       ✓ paper_analyst_promote_recommended=True")

        print("[m6b_smoke] [8/10] POST /strategies/{id}/promote-live")
        r = await client.post(
            f"/strategies/{strategy_id}/promote-live",
            json={"human_approval": True, "operator_note": "m6b smoke"},
        )
        if r.status_code != 200:
            print(f"       ✗ unexpected status {r.status_code}: {r.text}", file=sys.stderr)
            return 1
        body = r.json()
        if body.get("new_state") != StrategyState.LIVE_TRADING.value:
            print(
                f"       ✗ unexpected new_state={body.get('new_state')!r}",
                file=sys.stderr,
            )
            return 1
        promote_ar_id = body.get("agent_run_id")
        print(
            f"       ✓ new_state=live_trading promote_agent_run_id={promote_ar_id}"
        )

    print("[m6b_smoke] [9/10] final assertions: Strategy + Transition + AgentRun(promote_live)")
    async with SessionLocal() as session:
        s = (
            await session.execute(
                select(Strategy).where(Strategy.id == strategy_id)
            )
        ).scalar_one()
        meta_final = dict(s.metadata_json or {})

        transitions = (
            await session.execute(
                select(Transition)
                .where(
                    Transition.entity_type == EntityType.STRATEGY.value,
                    Transition.entity_id == strategy_id,
                    Transition.from_state == StrategyState.PAPER_TRADING.value,
                    Transition.to_state == StrategyState.LIVE_TRADING.value,
                )
            )
        ).scalars().all()

        promote_ars = (
            await session.execute(
                select(AgentRun).where(
                    AgentRun.strategy_id == strategy_id,
                    AgentRun.agent_name == "promote_live",
                    AgentRun.status == AgentRunStatus.DONE.value,
                )
            )
        ).scalars().all()

    if s.current_state != StrategyState.LIVE_TRADING.value:
        print(
            f"       ✗ current_state={s.current_state!r}, expected live_trading",
            file=sys.stderr,
        )
        return 1
    if meta_final.get("paper_analyst_promote_recommended") is not False:
        print(
            f"       ✗ paper_analyst_promote_recommended not cleared: "
            f"{meta_final.get('paper_analyst_promote_recommended')!r}",
            file=sys.stderr,
        )
        return 1
    promoted_at = meta_final.get("promoted_live_at")
    if not isinstance(promoted_at, str) or not promoted_at:
        print(
            f"       ✗ promoted_live_at missing/empty: {promoted_at!r}",
            file=sys.stderr,
        )
        return 1
    if len(transitions) != 1:
        print(
            f"       ✗ expected exactly 1 paper→live Transition, got {len(transitions)}",
            file=sys.stderr,
        )
        return 1
    t = transitions[0]
    if not (isinstance(t.payload, dict) and t.payload.get("human_approval") is True):
        print(
            f"       ✗ transition payload missing human_approval=True: {t.payload!r}",
            file=sys.stderr,
        )
        return 1
    if len(promote_ars) != 1:
        print(
            f"       ✗ expected exactly 1 promote_live AgentRun(DONE), got {len(promote_ars)}",
            file=sys.stderr,
        )
        return 1
    print(
        f"       ✓ current_state=live_trading "
        f"promoted_live_at={promoted_at} "
        f"transitions=1 promote_ar_done=1"
    )

    print("[m6b_smoke] [10/10] all assertions passed")
    print("[m6b_smoke] PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
