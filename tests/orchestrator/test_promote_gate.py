"""Promote-gate tests (Plan 009 WP4).

Drives run_promote_gate with a scripted fake fwbg client (no real backtests):
holdout + cost-stress pass/fail paths, a backtest-error path, and the
cumulative fail_count. Also covers the validate_and_apply integration
(promote transitions only when the gate passes).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest_asyncio
import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from fwbg_agents.agents.analyst import Promote
from fwbg_agents.orchestrator.promote_gate import run_promote_gate
from fwbg_agents.orchestrator.recommendations import validate_and_apply
from fwbg_agents.persistence.database import Base
from fwbg_agents.persistence.models import Strategy, StrategyState


def _run(sharpe=1.5, pf=1.5, annual=20.0, trades=120):
    return {
        "run_id": "gate_job",
        "status": "completed",
        "assets": {
            "EURUSD": {
                "unified_metrics": {
                    "sharpe": sharpe,
                    "profit_factor": pf,
                    "annual_return": annual,
                    "trades": trades,
                }
            }
        },
    }


class _GateFake:
    """Scripted fwbg client: one queued run_response per backtest, records the
    start_run kwargs so tests can assert the holdout window / cost multiplier."""

    def __init__(self, run_responses, *, progress_status="completed"):
        self._runs = list(run_responses)
        self._progress_status = progress_status
        self.start_calls: list[dict] = []

    async def list_runs(self):
        return []

    async def start_run(self, strategy_name, *, assets=None, asset_classes=None, **kwargs):
        self.start_calls.append({"assets": assets, **kwargs})
        return {"job_id": f"job_{len(self.start_calls)}", "status": "running"}

    async def get_progress(self, run_id):
        return {"status": self._progress_status}

    async def get_run(self, run_id):
        return self._runs.pop(0) if self._runs else _run()

    async def get_run_logs(self, run_id, *, limit=500):
        return []


@pytest_asyncio.fixture
async def gate_env(tmp_path, monkeypatch):
    from fwbg_agents.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path / "agents_data")
    monkeypatch.setattr(settings, "runner_poll_interval_seconds", 0.001)
    monkeypatch.setattr(settings, "runner_poll_timeout_seconds", 5.0)
    monkeypatch.setattr(settings, "holdout_months", 24)

    # Seed FOREX criteria with the two gate sections.
    settings.criteria_dir.mkdir(parents=True, exist_ok=True)
    (settings.criteria_dir / "FOREX.yaml").write_text(
        yaml.safe_dump(
            {
                "promote_holdout": {
                    "required_all": [
                        {"annual_return": "> 0"},
                        {"sharpe": ">= 1.0"},
                        {"profit_factor": ">= 1.3"},
                        {"trades": ">= 60"},
                    ]
                },
                "promote_cost_stress": {
                    "required_all": [
                        {"annual_return": "> 0"},
                        {"profit_factor": ">= 1.2"},
                    ]
                },
            }
        )
    )

    db_url = f"sqlite+aiosqlite:///{tmp_path}/gate.db"
    engine = create_async_engine(db_url, future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as setup:
        now = datetime.now(UTC)
        s = Strategy(
            slug="demo_v1",
            current_state=StrategyState.BACKTESTED.value,
            iteration_count=0,
            asset_class="FOREX",
            strategy_family="ORB",
            created_at=now,
            updated_at=now,
        )
        setup.add(s)
        await setup.commit()
        sid = s.id

    it_dir = settings.data_dir / "strategies" / "demo_v1" / "iteration_001"
    it_dir.mkdir(parents=True, exist_ok=True)
    (it_dir / "fwbg_results.json").write_text(
        json.dumps({"assets": {"EURUSD": {"unified_metrics": {"sharpe": 2.0}}}})
    )
    yield Session, sid, settings
    await engine.dispose()


async def test_gate_passes_when_both_runs_clear(gate_env):
    Session, sid, _ = gate_env
    fake = _GateFake([_run(), _run()])  # holdout, cost-stress both good
    async with Session() as session:
        strat = (await session.execute(select(Strategy).where(Strategy.id == sid))).scalar_one()
        result = await run_promote_gate(session, strat, fwbg_client=fake)

    assert result.passed is True
    assert result.fail_count == 0
    assert [r.label for r in result.runs] == ["holdout", "cost_stress"]
    # holdout run carried a date window; cost-stress run carried the multiplier.
    assert fake.start_calls[0].get("start_date") and fake.start_calls[0].get("end_date")
    assert fake.start_calls[1].get("cost_multiplier") == 2.0


async def test_gate_fails_on_weak_holdout(gate_env):
    Session, sid, _ = gate_env
    # Holdout sharpe below 1.0 → fails; cost-stress fine but gate still fails.
    fake = _GateFake([_run(sharpe=0.4), _run()])
    async with Session() as session:
        strat = (await session.execute(select(Strategy).where(Strategy.id == sid))).scalar_one()
        result = await run_promote_gate(session, strat, fwbg_client=fake)

    assert result.passed is False
    assert result.fail_count == 1
    holdout = next(r for r in result.runs if r.label == "holdout")
    assert not holdout.passed
    assert any("sharpe" in f for f in holdout.failures)


async def test_gate_fails_on_cost_stress(gate_env):
    Session, sid, _ = gate_env
    # Cost-stress profit_factor below 1.2 → fails.
    fake = _GateFake([_run(), _run(pf=1.1)])
    async with Session() as session:
        strat = (await session.execute(select(Strategy).where(Strategy.id == sid))).scalar_one()
        result = await run_promote_gate(session, strat, fwbg_client=fake)

    assert result.passed is False
    cost = next(r for r in result.runs if r.label == "cost_stress")
    assert not cost.passed
    assert any("profit_factor" in f for f in cost.failures)


async def test_gate_run_error_counts_as_failure(gate_env):
    Session, sid, _ = gate_env
    fake = _GateFake([_run(), _run()], progress_status="failed")  # runs never complete
    async with Session() as session:
        strat = (await session.execute(select(Strategy).where(Strategy.id == sid))).scalar_one()
        result = await run_promote_gate(session, strat, fwbg_client=fake)

    assert result.passed is False
    assert all(r.error for r in result.runs)


async def test_fail_count_accumulates_across_runs(gate_env):
    Session, sid, _ = gate_env
    async with Session() as session:
        strat = (await session.execute(select(Strategy).where(Strategy.id == sid))).scalar_one()
        await run_promote_gate(session, strat, fwbg_client=_GateFake([_run(sharpe=0.1), _run()]))
        result2 = await run_promote_gate(
            session, strat, fwbg_client=_GateFake([_run(sharpe=0.1), _run()])
        )
    assert result2.fail_count == 2


async def test_validate_and_apply_promote_blocked_by_failed_gate(gate_env):
    Session, sid, _ = gate_env
    rec = Promote(kind="promote", confidence=0.9, reasoning="looks great in-sample")
    fake = _GateFake([_run(sharpe=0.2), _run()])  # holdout fails
    async with Session() as session:
        strat = (await session.execute(select(Strategy).where(Strategy.id == sid))).scalar_one()
        transition = await validate_and_apply(
            session, strat, rec, metrics={"sharpe": 2.0}, fwbg_client=fake
        )
    assert transition is None  # no promotion
    async with Session() as v:
        strat = (await v.execute(select(Strategy).where(Strategy.id == sid))).scalar_one()
        assert strat.current_state == StrategyState.BACKTESTED.value


async def test_validate_and_apply_promote_passes_gate_and_transitions(gate_env):
    Session, sid, _ = gate_env
    rec = Promote(kind="promote", confidence=0.9, reasoning="robust across holdout + costs")
    # Both gate runs pass; the median criteria gate in transition_strategy also
    # needs the promoted metrics to clear FOREX backtest_to_paper — but that
    # section isn't seeded here, so seed a permissive one for the transition.
    _, _, settings = gate_env
    existing = yaml.safe_load((settings.criteria_dir / "FOREX.yaml").read_text())
    existing["backtest_to_paper"] = {"required_all": [{"sharpe": ">= 1.0"}]}
    (settings.criteria_dir / "FOREX.yaml").write_text(yaml.safe_dump(existing))

    fake = _GateFake([_run(), _run()])
    async with Session() as session:
        strat = (await session.execute(select(Strategy).where(Strategy.id == sid))).scalar_one()
        transition = await validate_and_apply(
            session, strat, rec, metrics={"sharpe": 2.0}, fwbg_client=fake
        )
    assert transition is not None
    async with Session() as v:
        strat = (await v.execute(select(Strategy).where(Strategy.id == sid))).scalar_one()
        assert strat.current_state == StrategyState.PAPER_TRADING.value
