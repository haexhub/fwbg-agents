"""Promote-gate tests (Plan 009 WP4 + Plan 010 WP2).

Drives run_promote_gate with a scripted fake fwbg client (no real backtests):
holdout + cost-stress pass/fail paths, a backtest-error path, the cumulative
fail_count, and the DSR check (Plan 010 WP2). Also covers the
validate_and_apply integration (promote transitions only when the gate
passes).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
import pytest_asyncio
import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from fwbg_agents.agents.analyst import Promote
from fwbg_agents.orchestrator.promote_gate import run_promote_gate
from fwbg_agents.orchestrator.recommendations import validate_and_apply
from fwbg_agents.orchestrator.research_card import ResearchCardError
from fwbg_agents.persistence.database import Base
from fwbg_agents.persistence.models import (
    AgentRun,
    AgentRunStatus,
    Strategy,
    StrategyState,
    TrialStat,
)
from fwbg_agents.tools.fwbg_client import FwbgClientError


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


# A holdout look's trade series that clears the DSR on a cold start (<2
# historical trial Sharpes): the check needs a readable series before it may
# skip on missing history.
_HOLDOUT_PNLS = [1.0, -0.5, 1.2, -0.4, 0.9, -0.6, 1.1, -0.3]


def _write_holdout_trades(settings, run_id: str, symbol: str, pnls: list[float]) -> None:
    """The register holdout look's layout: config.json with the evaluated
    holdout block and trades.json, no fold_results.json."""
    sym_dir = settings.fwbg_test_results_dir / run_id / "grid_details" / symbol
    sym_dir.mkdir(parents=True, exist_ok=True)
    config = {"status": "ok", "holdout": {"cutoff": "2024-01-01", "evaluated": True}}
    (sym_dir / "config.json").write_text(json.dumps(config))
    (sym_dir / "trades.json").write_text(json.dumps({"tr_trace": pnls, "trades_detailed": []}))


class _GateFake:
    """Scripted fwbg client: one queued run_response per backtest, records the
    start_run kwargs so tests can assert the holdout look / cost multiplier.
    A holdout start writes ``holdout_pnls`` as the run's trades.json, like
    fwbg's holdout look; ``None`` writes nothing."""

    def __init__(
        self,
        run_responses,
        *,
        progress_status="completed",
        holdout_pnls=_HOLDOUT_PNLS,
        trials=(),
        card_error=None,
        card_response=None,
        start_error=None,
    ):
        self._runs = list(run_responses)
        self._progress_status = progress_status
        self._holdout_pnls = holdout_pnls
        self._trials = list(trials)
        self._card_error = card_error
        self._card_response = card_response
        self._start_error = start_error
        self.start_calls: list[dict] = []
        self.card_calls: list[str] = []

    async def list_runs(self):
        return []

    async def get_research_card(self, research_id):
        self.card_calls.append(research_id)
        if self._card_error is not None:
            raise self._card_error
        if self._card_response is not None:
            return self._card_response
        return {"card": {"id": research_id}, "trials": self._trials}

    async def start_run(self, strategy_name, *, assets=None, asset_classes=None, **kwargs):
        self.start_calls.append({"assets": assets, **kwargs})
        if self._start_error is not None:
            raise self._start_error
        job_id = f"job_{len(self.start_calls)}"
        if kwargs.get("holdout") and self._holdout_pnls is not None:
            from fwbg_agents.config import settings

            _write_holdout_trades(settings, job_id, "EURUSD", self._holdout_pnls)
        return {"job_id": job_id, "status": "running"}

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
    monkeypatch.setattr(settings, "fwbg_test_results_dir", tmp_path / "test_results")
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
            metadata_json={"research_id": "S-0001"},
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
    assert [r.label for r in result.runs] == ["holdout", "cost_stress", "dsr"]
    assert fake.start_calls[1].get("cost_multiplier") == 2.0
    # Cold start: the holdout series is read, but <2 historical trial Sharpes.
    dsr = next(r for r in result.runs if r.label == "dsr")
    assert dsr.passed is True


async def test_holdout_is_a_register_holdout_look_without_a_window(gate_env):
    """fwbg develops up to the card's cutoff and evaluates once after it; a
    window from the gate would fight the card's split."""
    Session, sid, _ = gate_env
    fake = _GateFake([_run(), _run()])
    async with Session() as session:
        strat = (await session.execute(select(Strategy).where(Strategy.id == sid))).scalar_one()
        await run_promote_gate(session, strat, fwbg_client=fake)

    holdout, cost = fake.start_calls
    assert holdout["holdout"] is True
    assert holdout.get("start_date") is None and holdout.get("end_date") is None
    assert holdout["note"] == "promote gate holdout look"
    # Cost stress stays a development run: no holdout look, no window.
    assert not cost.get("holdout")
    assert cost.get("start_date") is None and cost.get("end_date") is None
    assert cost["note"] == "promote gate cost stress x2"


async def test_first_holdout_look_sends_no_reason(gate_env):
    Session, sid, _ = gate_env
    fake = _GateFake([_run(), _run()], trials=[{"kind": "iteration", "status": "ok"}])
    async with Session() as session:
        strat = (await session.execute(select(Strategy).where(Strategy.id == sid))).scalar_one()
        await run_promote_gate(session, strat, fwbg_client=fake)

    assert fake.card_calls == ["S-0001"]
    assert [c.get("reason") for c in fake.start_calls] == [None, None]


async def test_further_holdout_look_sends_a_reason(gate_env):
    """Any earlier look counts, a failed one too (fwbg's rule)."""
    Session, sid, _ = gate_env
    fake = _GateFake(
        [_run(), _run()],
        trials=[{"kind": "iteration", "status": "ok"}, {"kind": "holdout", "status": "failed"}],
    )
    async with Session() as session:
        strat = (await session.execute(select(Strategy).where(Strategy.id == sid))).scalar_one()
        await run_promote_gate(session, strat, fwbg_client=fake)

    holdout, cost = fake.start_calls
    assert holdout["reason"] == "promote gate: further holdout look (demo_v1)"
    assert cost.get("reason") is None


async def test_unreadable_card_fails_the_holdout_without_running_it(gate_env):
    """Without the card's trials the gate cannot know whether a look needs a
    reason: the holdout fails closed instead of guessing."""
    Session, sid, _ = gate_env
    fake = _GateFake([_run()], card_error=FwbgClientError(500, "register unreadable"))
    async with Session() as session:
        strat = (await session.execute(select(Strategy).where(Strategy.id == sid))).scalar_one()
        result = await run_promote_gate(session, strat, fwbg_client=fake)

    assert result.passed is False
    assert result.fail_count == 1
    holdout = next(r for r in result.runs if r.label == "holdout")
    assert holdout.passed is False and holdout.fwbg_run_id is None
    assert "register unreadable" in (holdout.error or "")
    assert [bool(c.get("holdout")) for c in fake.start_calls] == [False]  # only cost stress ran


async def test_malformed_card_fails_the_holdout_without_aborting_the_gate(gate_env):
    Session, sid, _ = gate_env
    fake = _GateFake([_run()], card_response=["not", "a", "card"])
    async with Session() as session:
        strat = (await session.execute(select(Strategy).where(Strategy.id == sid))).scalar_one()
        result = await run_promote_gate(session, strat, fwbg_client=fake)

    assert result.passed is False
    holdout = next(r for r in result.runs if r.label == "holdout")
    assert holdout.passed is False and holdout.fwbg_run_id is None
    assert [r.label for r in result.runs] == ["holdout", "cost_stress", "dsr"]


def _universe_run(sharpe=1.5):
    """EURUSD with metrics; GBPUSD's development found no edge; USDJPY was
    looked at but made no trades after the cutoff (fwbg's get_run shape)."""
    run = _run(sharpe=sharpe)
    run["assets"]["GBPUSD"] = {"status": "no_edge", "unified_metrics": {}}
    run["assets"]["USDJPY"] = {"status": "no_unified_trades", "unified_metrics": {}}
    return run


async def test_holdout_symbol_without_holdout_metrics_counts_as_no_edge(gate_env):
    """A universe member without holdout metrics showed no edge after the
    cutoff; leaving it out would let the symbols that worked carry the median."""
    Session, sid, _ = gate_env
    fake = _GateFake([_universe_run(), _universe_run()])
    async with Session() as session:
        strat = (await session.execute(select(Strategy).where(Strategy.id == sid))).scalar_one()
        result = await run_promote_gate(session, strat, fwbg_client=fake)

    holdout = next(r for r in result.runs if r.label == "holdout")
    assert holdout.metrics["sharpe"] == 0.0  # median of 1.5, 0.0, 0.0
    assert holdout.metrics["profit_factor"] == 1.5  # other metrics: EURUSD only
    assert holdout.passed is False
    # The cost-stress development run keeps the shared rule (D7): left out.
    cost = next(r for r in result.runs if r.label == "cost_stress")
    assert cost.metrics["sharpe"] == 1.5


async def test_gate_refusal_counts_as_failed_gate_run(gate_env):
    Session, sid, _ = gate_env
    body = json.dumps(
        {
            "detail": {
                "message": "research gate refused the run",
                "denials": [
                    {"code": "holdout_repeat", "status": 409, "message": "already used"},
                ],
            }
        }
    )
    fake = _GateFake([], start_error=FwbgClientError(409, body))
    async with Session() as session:
        strat = (await session.execute(select(Strategy).where(Strategy.id == sid))).scalar_one()
        result = await run_promote_gate(session, strat, fwbg_client=fake)

    assert result.passed is False
    assert result.fail_count == 1
    holdout = next(r for r in result.runs if r.label == "holdout")
    assert holdout.passed is False
    assert "holdout_repeat" in (holdout.error or "")


async def test_missing_card_is_a_configuration_error_not_a_gate_failure(gate_env):
    """A lineage without a card cannot run any gate backtest; that is a setup
    problem, so it must not use up the lineage's attempt budget."""
    Session, sid, settings = gate_env
    sidecar = settings.data_dir / "strategies" / "demo_v1" / "promote_gate_results.json"
    sidecar.write_text(json.dumps({"fail_count": 1}))
    async with Session() as session:
        strat = (await session.execute(select(Strategy).where(Strategy.id == sid))).scalar_one()
        strat.metadata_json = {}  # no card id, and the root has no hypothesis.json
        await session.commit()
        fake = _GateFake([_run(), _run()])
        with pytest.raises(ResearchCardError, match=r"hypothesis\.json"):
            await run_promote_gate(session, strat, fwbg_client=fake)

    assert fake.start_calls == []
    assert json.loads(sidecar.read_text())["fail_count"] == 1
    async with Session() as session:
        ar = (
            await session.execute(select(AgentRun).where(AgentRun.agent_name == "promote_gate"))
        ).scalar_one()
        assert ar.status == AgentRunStatus.FAILED.value
        assert "hypothesis.json" in (ar.error or "")


async def test_holdout_look_is_not_recorded_as_a_trial(gate_env):
    """A holdout look evaluates one frozen setting, it selects nothing (fwbg
    keeps looks out of its trial count too); the cost-stress run is recorded."""
    Session, sid, _ = gate_env
    fake = _GateFake([_run(), _run()])
    async with Session() as session:
        strat = (await session.execute(select(Strategy).where(Strategy.id == sid))).scalar_one()
        await run_promote_gate(session, strat, fwbg_client=fake)
        recorded = (await session.execute(select(TrialStat.run_id))).scalars().all()

    assert recorded == ["job_2"]  # cost stress only; job_1 is the holdout look


async def test_gate_runs_carry_the_lineage_card_and_a_note(gate_env):
    Session, sid, _ = gate_env
    fake = _GateFake([_run(), _run()])
    async with Session() as session:
        strat = (await session.execute(select(Strategy).where(Strategy.id == sid))).scalar_one()
        await run_promote_gate(session, strat, fwbg_client=fake)

    assert [c["research_id"] for c in fake.start_calls] == ["S-0001", "S-0001"]
    assert all(c["note"].strip() for c in fake.start_calls)


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
    holdout = next(r for r in result.runs if r.label == "holdout")
    cost = next(r for r in result.runs if r.label == "cost_stress")
    assert holdout.error and cost.error
    # DSR has no holdout job_id to inspect (the backtest itself errored); the
    # holdout's own failure already fails the gate.
    dsr = next(r for r in result.runs if r.label == "dsr")
    assert dsr.passed is True


async def test_fail_count_accumulates_across_runs(gate_env):
    Session, sid, _ = gate_env
    async with Session() as session:
        strat = (await session.execute(select(Strategy).where(Strategy.id == sid))).scalar_one()
        await run_promote_gate(session, strat, fwbg_client=_GateFake([_run(sharpe=0.1), _run()]))
        result2 = await run_promote_gate(
            session, strat, fwbg_client=_GateFake([_run(sharpe=0.1), _run()])
        )
    assert result2.fail_count == 2


async def test_fail_count_shared_across_lineage_children(gate_env):
    """Two children of one root share one fail_count — a failure recorded via
    child A is visible when child B runs the gate (Plan 014)."""
    Session, root_sid, _ = gate_env
    async with Session() as session:
        now = datetime.now(UTC)
        child_a = Strategy(
            slug="demo_v1_child_a",
            current_state=StrategyState.BACKTESTED.value,
            iteration_count=1,
            parent_strategy_id=root_sid,
            asset_class="FOREX",
            strategy_family="ORB",
            created_at=now,
            updated_at=now,
        )
        child_b = Strategy(
            slug="demo_v1_child_b",
            current_state=StrategyState.BACKTESTED.value,
            iteration_count=1,
            parent_strategy_id=root_sid,
            asset_class="FOREX",
            strategy_family="ORB",
            created_at=now,
            updated_at=now,
        )
        session.add_all([child_a, child_b])
        await session.commit()
        child_a_id, child_b_id = child_a.id, child_b.id

    async with Session() as session:
        strat_a = (
            await session.execute(select(Strategy).where(Strategy.id == child_a_id))
        ).scalar_one()
        result_a = await run_promote_gate(
            session, strat_a, fwbg_client=_GateFake([_run(sharpe=0.1), _run()])
        )
    assert result_a.fail_count == 1

    async with Session() as session:
        strat_b = (
            await session.execute(select(Strategy).where(Strategy.id == child_b_id))
        ).scalar_one()
        result_b = await run_promote_gate(session, strat_b, fwbg_client=_GateFake([_run(), _run()]))
    # Child B's own gate run passes, but the cumulative counter is shared
    # with the lineage root — child A's earlier failure is still visible.
    assert result_b.passed is True
    assert result_b.fail_count == 1


async def test_budget_short_circuit_after_max_attempts(gate_env, monkeypatch):
    Session, sid, settings = gate_env
    monkeypatch.setattr(settings, "promote_max_attempts", 2)

    async with Session() as session:
        strat = (await session.execute(select(Strategy).where(Strategy.id == sid))).scalar_one()
        await run_promote_gate(
            session, strat, fwbg_client=_GateFake([_run(sharpe=0.1), _run()])
        )  # fail 1

    async with Session() as session:
        strat = (await session.execute(select(Strategy).where(Strategy.id == sid))).scalar_one()
        await run_promote_gate(
            session, strat, fwbg_client=_GateFake([_run(sharpe=0.1), _run()])
        )  # fail 2 == budget

    fake3 = _GateFake([_run(), _run()])  # would pass, but budget already exhausted
    async with Session() as session:
        strat = (await session.execute(select(Strategy).where(Strategy.id == sid))).scalar_one()
        result3 = await run_promote_gate(session, strat, fwbg_client=fake3)

    assert result3.passed is False
    assert result3.fail_count == 2
    assert result3.runs == []
    assert fake3.start_calls == []  # fwbg client never invoked — no backtest ran


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


def _write_run_trades(settings, run_id: str, symbol: str, pnls: list[float]) -> None:
    sym_dir = settings.fwbg_test_results_dir / run_id / "grid_details" / symbol
    sym_dir.mkdir(parents=True, exist_ok=True)
    (sym_dir / "fold_results.json").write_text(
        json.dumps(
            {
                "walk_forward": {
                    "fold_details": [
                        {
                            "test_trades_detail": [
                                {"pnl_raw": p, "entry_time": "2024-01-01T00:00:00"} for p in pnls
                            ]
                        }
                    ]
                }
            }
        )
    )


async def test_gate_fails_on_low_dsr_despite_clean_holdout_and_cost_stress(gate_env):
    """A weak-edge holdout trade series must block promotion via DSR even
    when the criteria-based holdout/cost-stress checks both pass."""
    Session, sid, settings = gate_env

    # Two historical strategies establish the cross-trial SR variance sample.
    async with Session() as session:
        now = datetime.now(UTC)
        for slug in ("prior_a", "prior_b"):
            session.add(
                Strategy(
                    slug=slug,
                    current_state=StrategyState.BACKTESTED.value,
                    iteration_count=1,
                    asset_class="FOREX",
                    strategy_family="ORB",
                    created_at=now,
                    updated_at=now,
                )
            )
        session.add_all(
            [
                TrialStat(
                    run_id="hist_run_a",
                    strategy_family="ORB",
                    n_trials=1,
                    trade_sharpe=0.1,
                    n_trades=10,
                    created_at=now,
                ),
                TrialStat(
                    run_id="hist_run_b",
                    strategy_family="ORB",
                    n_trials=1,
                    trade_sharpe=-0.1,
                    n_trades=10,
                    created_at=now,
                ),
            ]
        )
        await session.commit()

    for slug, run_id, pnls in [
        ("prior_a", "hist_run_a", [5, -4, 6, -5, 4, -3, 5, -4, 6, -5]),
        ("prior_b", "hist_run_b", [-2, 3, -1, 2, -3, 1, -2, 3, -1, 2]),
    ]:
        it_dir = settings.data_dir / "strategies" / slug / "iteration_001"
        it_dir.mkdir(parents=True, exist_ok=True)
        (it_dir / "fwbg_results.json").write_text(
            json.dumps({"run_id": run_id, "assets": {"EURUSD": {"unified_metrics": {}}}})
        )
        _write_run_trades(settings, run_id, "EURUSD", pnls)

    # The holdout look (job_1) gets a weak-edge trade series in the register
    # layout (trades.json): non-trivial per-trade mean but noisy enough that
    # DSR < 0.95.
    holdout_pnls = [
        10,
        -8,
        12,
        -9,
        11,
        -7,
        9,
        -10,
        13,
        -8,
        10,
        -9,
        11,
        -8,
        9,
        -7,
        8,
        -9,
        10,
        -8,
    ]

    fake = _GateFake([_run(), _run()], holdout_pnls=holdout_pnls)  # criteria checks pass
    async with Session() as session:
        strat = (await session.execute(select(Strategy).where(Strategy.id == sid))).scalar_one()
        result = await run_promote_gate(session, strat, fwbg_client=fake)

    assert result.passed is False
    dsr = next(r for r in result.runs if r.label == "dsr")
    assert dsr.passed is False
    assert dsr.metrics["dsr"] < settings.dsr_min
    assert result.dsr == dsr.metrics["dsr"]
    assert result.n_trials is not None and result.n_trials >= 2


async def test_gate_fails_closed_on_literal_nan_holdout_trade(gate_env):
    Session, sid, _ = gate_env
    now = datetime.now(UTC)
    async with Session() as session:
        session.add_all(
            [
                TrialStat(
                    run_id="prior_1",
                    strategy_family="ORB",
                    n_trials=1,
                    trade_sharpe=0.1,
                    n_trades=10,
                    created_at=now,
                ),
                TrialStat(
                    run_id="prior_2",
                    strategy_family="ORB",
                    n_trials=1,
                    trade_sharpe=-0.1,
                    n_trades=10,
                    created_at=now,
                ),
            ]
        )
        await session.commit()

    async with Session() as session:
        strategy = await session.get(Strategy, sid)
        assert strategy is not None
        result = await run_promote_gate(
            session,
            strategy,
            fwbg_client=_GateFake([_run(), _run()], holdout_pnls=[1.0, float("nan"), -1.0]),
        )

    dsr_run = next(run for run in result.runs if run.label == "dsr")
    assert result.passed is False
    assert dsr_run.passed is False
    assert dsr_run.failures == ["dsr is NaN — non-finite inputs; failing closed"]


async def test_dsr_reads_the_holdout_look_trades(gate_env):
    """The DSR is computed from the holdout look's trades.json tr_trace."""
    Session, sid, _ = gate_env
    await _seed_trial_sharpes(Session)
    fake = _GateFake([_run(), _run()], holdout_pnls=[2.0, -0.1, 1.9, 2.1, -0.2, 2.2, 1.8, 2.0])
    async with Session() as session:
        strat = (await session.execute(select(Strategy).where(Strategy.id == sid))).scalar_one()
        result = await run_promote_gate(session, strat, fwbg_client=fake)

    dsr = next(r for r in result.runs if r.label == "dsr")
    assert dsr.fwbg_run_id == "job_1"
    assert dsr.metrics["dsr"] == result.dsr
    assert result.dsr is not None and result.dsr > 0.5


async def test_dsr_fails_closed_on_a_holdout_look_without_trades(gate_env):
    """No holdout trades means the DSR cannot vouch for the look: fail, even on
    a cold start."""
    Session, sid, _ = gate_env
    fake = _GateFake([_run(), _run()], holdout_pnls=None)  # no trades.json written
    async with Session() as session:
        strat = (await session.execute(select(Strategy).where(Strategy.id == sid))).scalar_one()
        result = await run_promote_gate(session, strat, fwbg_client=fake)

    assert result.passed is False
    dsr = next(r for r in result.runs if r.label == "dsr")
    assert dsr.passed is False
    assert any("made no trades" in f for f in dsr.failures)


async def test_dsr_fails_closed_on_a_single_holdout_trade(gate_env):
    """One trade has no per-trade Sharpe: the DSR cannot judge it."""
    Session, sid, _ = gate_env
    fake = _GateFake([_run(), _run()], holdout_pnls=[1.0])
    async with Session() as session:
        strat = (await session.execute(select(Strategy).where(Strategy.id == sid))).scalar_one()
        result = await run_promote_gate(session, strat, fwbg_client=fake)

    dsr = next(r for r in result.runs if r.label == "dsr")
    assert dsr.passed is False
    assert any("too short or flat" in f for f in dsr.failures)


async def test_dsr_fails_closed_on_an_unreadable_holdout_trade_file(gate_env):
    Session, sid, settings = gate_env
    fake = _GateFake([_run(), _run()])
    # A second symbol's trades.json is corrupt: a partial series would judge
    # only the symbols that happened to be readable.
    bad = settings.fwbg_test_results_dir / "job_1" / "grid_details" / "GBPUSD"
    bad.mkdir(parents=True)
    (bad / "trades.json").write_text("{not json")
    async with Session() as session:
        strat = (await session.execute(select(Strategy).where(Strategy.id == sid))).scalar_one()
        result = await run_promote_gate(session, strat, fwbg_client=fake)

    dsr = next(r for r in result.runs if r.label == "dsr")
    assert dsr.passed is False
    assert any("unreadable or not from an evaluated holdout look" in f for f in dsr.failures)


async def _seed_trial_sharpes(Session) -> None:
    now = datetime.now(UTC)
    async with Session() as session:
        session.add_all(
            [
                TrialStat(
                    run_id=f"prior_{i}",
                    strategy_family="ORB",
                    n_trials=1,
                    trade_sharpe=sr,
                    n_trades=10,
                    created_at=now,
                )
                for i, sr in enumerate((0.1, -0.1))
            ]
        )
        await session.commit()


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
