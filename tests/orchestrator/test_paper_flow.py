"""Paper-flow orchestrator tests (M6b Task 5).

`paper_analyze` is the entry point that loads on-disk paper-trading
telemetry, runs the PaperAnalyst, persists the recommendation as a
sidecar JSON, and flags Strategy.metadata_json for promotion / abandon
review. It does NOT transition state — only humans do (M7).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio
import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from fwbg_agents.agents.paper_analyst import (
    AbandonPaper,
    ContinueObservation,
    PromotePaperToLive,
)
from fwbg_agents.persistence.database import Base
from fwbg_agents.persistence.models import (
    AgentRun,
    AgentRunStatus,
    Strategy,
    StrategyState,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _write_paper_criteria(criteria_dir, asset_class: str = "forex"):
    paper_dir = criteria_dir / "paper"
    paper_dir.mkdir(parents=True, exist_ok=True)
    (paper_dir / f"{asset_class}.yaml").write_text(
        yaml.safe_dump(
            {
                "paper_to_live": {
                    "required_all": [
                        {"sharpe_paper": ">= 0.5"},
                        {"trades_total": ">= 1"},
                    ]
                }
            }
        )
    )


def _seed_account_trades(fwbg_data_dir, slug: str):
    """Write trades.jsonl + status.json under <fwbg_data_dir>/account-trades/<slug>/."""
    base = fwbg_data_dir / "account-trades" / slug
    base.mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC)
    trade_lines = [
        json.dumps(
            {
                "entry_time": (now - timedelta(days=2)).isoformat(),
                "pnl_pct": 0.012,
            }
        ),
        json.dumps(
            {
                "entry_time": (now - timedelta(days=1)).isoformat(),
                "pnl_pct": -0.005,
            }
        ),
        json.dumps(
            {
                "entry_time": now.isoformat(),
                "pnl_pct": 0.020,
            }
        ),
    ]
    (base / "trades.jsonl").write_text("\n".join(trade_lines) + "\n")
    (base / "status.json").write_text(
        json.dumps(
            {
                "current_equity": 10250.0,
                "starting_equity": 10000.0,
                "equity_curve_sample": [
                    {"t": (now - timedelta(days=2)).isoformat(), "equity": 10000.0},
                    {"t": (now - timedelta(days=1)).isoformat(), "equity": 10120.0},
                    {"t": now.isoformat(), "equity": 10250.0},
                ],
            }
        )
    )


@pytest_asyncio.fixture
async def db_and_paper(tmp_path, monkeypatch):
    """Create a Strategy in PAPER_TRADING with on-disk telemetry under tmp_path.

    Returns (Session, strategy_id, settings_stub, tmp_path).
    `_write_paper_criteria` already wrote the FOREX paper YAML into
    settings.data_dir/criteria/paper/.
    """
    from fwbg_agents.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path / "agents-data")

    _write_paper_criteria(settings.criteria_dir, asset_class="forex")

    db_url = f"sqlite+aiosqlite:///{tmp_path}/paper.db"
    engine = create_async_engine(db_url, future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async with Session() as setup:
        now = datetime.now(UTC)
        s = Strategy(
            slug="demo_paper_v1",
            current_state=StrategyState.PAPER_TRADING.value,
            iteration_count=1,
            asset_class="forex",
            strategy_family="ORB",
            paper_phase_target_days=90,
            metadata_json={},
            created_at=now,
            updated_at=now,
        )
        setup.add(s)
        await setup.commit()
        await setup.refresh(s)
        sid = s.id

    fwbg_data_dir = tmp_path / "fwbg-data"
    fwbg_data_dir.mkdir(parents=True, exist_ok=True)
    settings_stub = SimpleNamespace(fwbg_data_dir=fwbg_data_dir)

    yield Session, sid, settings_stub, tmp_path
    await engine.dispose()


class _StubAnalyst:
    """Stand-in for PaperAnalyst that returns a pre-built outcome.

    Skips pydantic-ai entirely — the orchestrator only depends on
    `analyze_sync(**kwargs) -> PromotePaperToLive | AbandonPaper | ContinueObservation`.
    """

    def __init__(self, outcome=None, *, raises: Exception | None = None):
        self.outcome = outcome
        self.raises = raises
        self.calls: list[dict] = []

    def analyze_sync(self, **kwargs):
        self.calls.append(kwargs)
        if self.raises is not None:
            raise self.raises
        return self.outcome


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_paper_analyze_raises_when_strategy_not_in_paper_trading(
    db_and_paper,
):
    from fwbg_agents.orchestrator.paper_flow import PaperFlowError, paper_analyze

    Session, sid, settings_stub, _tmp = db_and_paper

    async with Session() as session:
        s = (await session.execute(select(Strategy).where(Strategy.id == sid))).scalar_one()
        s.current_state = StrategyState.PROPOSED.value
        await session.commit()

    async with Session() as session:
        with pytest.raises(PaperFlowError):
            await paper_analyze(
                sid,
                session,
                settings=settings_stub,
                analyst=_StubAnalyst(),
            )


async def test_paper_analyze_raises_when_no_on_disk_data(db_and_paper):
    from fwbg_agents.orchestrator.paper_flow import PaperFlowError, paper_analyze

    Session, sid, settings_stub, _tmp = db_and_paper
    # NO _seed_account_trades — telemetry files are absent.

    async with Session() as session:
        with pytest.raises(PaperFlowError):
            await paper_analyze(
                sid,
                session,
                settings=settings_stub,
                analyst=_StubAnalyst(),
            )


async def test_paper_analyze_promote_outcome_sets_metadata_and_marks_done(
    db_and_paper,
):
    from fwbg_agents.orchestrator.paper_flow import paper_analyze

    Session, sid, settings_stub, _tmp = db_and_paper
    _seed_account_trades(settings_stub.fwbg_data_dir, "demo_paper_v1")
    stub = _StubAnalyst(outcome=PromotePaperToLive(rationale="all gates clear"))

    async with Session() as session:
        ar = await paper_analyze(sid, session, settings=settings_stub, analyst=stub)
        assert ar.status == AgentRunStatus.DONE.value
        assert ar.output_artifact_path is not None
        assert ar.ended_at is not None

    async with Session() as v:
        s = (await v.execute(select(Strategy).where(Strategy.id == sid))).scalar_one()
        assert s.metadata_json.get("paper_analyst_promote_recommended") is True
        # No state transition — paper_analyze never touches current_state.
        assert s.current_state == StrategyState.PAPER_TRADING.value
        runs = (await v.execute(select(AgentRun))).scalars().all()
        assert len(runs) == 1
        assert runs[0].agent_name == "paper_analyst"


async def test_paper_analyze_abandon_outcome_sets_metadata_with_post_mortem_path(
    db_and_paper,
):
    from fwbg_agents.orchestrator.paper_flow import paper_analyze

    Session, sid, settings_stub, _tmp = db_and_paper
    _seed_account_trades(settings_stub.fwbg_data_dir, "demo_paper_v1")
    stub = _StubAnalyst(
        outcome=AbandonPaper(
            rationale="no edge under live spreads",
            post_mortem_path="x.md",
        )
    )

    async with Session() as session:
        ar = await paper_analyze(sid, session, settings=settings_stub, analyst=stub)
        assert ar.status == AgentRunStatus.DONE.value

    async with Session() as v:
        s = (await v.execute(select(Strategy).where(Strategy.id == sid))).scalar_one()
        assert s.metadata_json.get("paper_analyst_abandon_recommended") is True
        assert s.metadata_json.get("paper_analyst_post_mortem_path") == "x.md"
        assert "paper_analyst_promote_recommended" not in s.metadata_json


async def test_paper_analyze_continue_outcome_leaves_metadata_unchanged(
    db_and_paper,
):
    from fwbg_agents.orchestrator.paper_flow import paper_analyze

    Session, sid, settings_stub, _tmp = db_and_paper
    _seed_account_trades(settings_stub.fwbg_data_dir, "demo_paper_v1")

    # Preload metadata with a known marker so we can confirm it's untouched.
    async with Session() as session:
        s = (await session.execute(select(Strategy).where(Strategy.id == sid))).scalar_one()
        s.metadata_json = {"pre_existing_marker": "keep_me"}
        await session.commit()

    stub = _StubAnalyst(outcome=ContinueObservation(rationale="need more samples", stale=False))
    async with Session() as session:
        ar = await paper_analyze(sid, session, settings=settings_stub, analyst=stub)
        assert ar.status == AgentRunStatus.DONE.value

    async with Session() as v:
        s = (await v.execute(select(Strategy).where(Strategy.id == sid))).scalar_one()
        # Untouched — only the pre-existing marker, no Continue-specific keys.
        assert s.metadata_json == {"pre_existing_marker": "keep_me"}


async def test_paper_analyze_marks_agent_run_failed_on_exception(db_and_paper):
    from fwbg_agents.orchestrator.paper_flow import paper_analyze

    Session, sid, settings_stub, _tmp = db_and_paper
    _seed_account_trades(settings_stub.fwbg_data_dir, "demo_paper_v1")
    boom = RuntimeError("analyst exploded")
    stub = _StubAnalyst(raises=boom)

    async with Session() as session:
        with pytest.raises(RuntimeError, match="analyst exploded"):
            await paper_analyze(sid, session, settings=settings_stub, analyst=stub)

    async with Session() as v:
        runs = (await v.execute(select(AgentRun))).scalars().all()
        assert len(runs) == 1
        ar = runs[0]
        assert ar.status == AgentRunStatus.FAILED.value
        assert ar.error is not None
        assert "analyst exploded" in ar.error
        assert ar.ended_at is not None
