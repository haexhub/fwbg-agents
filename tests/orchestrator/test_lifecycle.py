"""Lifecycle state-machine tests.

Coverage:
- valid happy path proposed → backtested → paper_trading → live_trading
- invalid sprong (proposed → live_trading) rejected
- abandon transition needs `post_mortem_path` in payload
- abandon writes filesystem entry
- paper → live demands `human_approval=True`
- backtested → paper enforces criteria YAML if present
- transition rows are append-only (no UPDATE/DELETE in lifecycle code)

The lifecycle module is the only legal way to change `strategy.current_state`;
each transition produces one immutable `transition` row.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import pytest_asyncio
import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from fwbg_agents.orchestrator.lifecycle import (
    InvalidTransitionError,
    plugin_dir,
    strategy_dir,
    transition_plugin,
    transition_strategy,
)
from fwbg_agents.persistence.database import Base
from fwbg_agents.persistence.models import (
    Plugin,
    PluginKind,
    PluginState,
    Strategy,
    StrategyState,
    Transition,
)

# ----- fixtures --------------------------------------------------------------


@pytest_asyncio.fixture
async def db_session(tmp_path):
    """Fresh sqlite file + AsyncSession per test. No alembic — direct create_all."""
    db_url = f"sqlite+aiosqlite:///{tmp_path}/test.db"
    engine = create_async_engine(db_url, echo=False, future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    SessionMaker = async_sessionmaker(engine, expire_on_commit=False)
    async with SessionMaker() as session:
        yield session
    await engine.dispose()


@pytest_asyncio.fixture
async def proposed_strategy(db_session, tmp_path, monkeypatch):
    """A Strategy row in `proposed` state, with data_dir pointed at tmp."""
    from fwbg_agents.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    now = datetime.now(UTC)
    s = Strategy(
        slug="orb_dax_m15",
        current_state=StrategyState.PROPOSED.value,
        iteration_count=0,
        asset_class="INDEX",
        strategy_family="ORB",
        created_at=now,
        updated_at=now,
    )
    db_session.add(s)
    await db_session.commit()
    await db_session.refresh(s)
    return s


@pytest_asyncio.fixture
async def specified_plugin(db_session, tmp_path, monkeypatch):
    from fwbg_agents.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    now = datetime.now(UTC)
    p = Plugin(
        slug="atr_v2",
        current_state=PluginState.SPECIFIED.value,
        kind=PluginKind.INDICATOR.value,
        created_at=now,
        updated_at=now,
    )
    db_session.add(p)
    await db_session.commit()
    await db_session.refresh(p)
    return p


# ----- happy-path strategy transitions ---------------------------------------


async def test_strategy_proposed_to_backtested_succeeds(db_session, proposed_strategy):
    """Smallest possible transition — no metrics, no guard logic required."""
    await transition_strategy(
        db_session,
        proposed_strategy,
        StrategyState.BACKTESTED,
        reason="initial backtest submitted",
    )
    assert proposed_strategy.current_state == StrategyState.BACKTESTED.value

    rows = (
        (
            await db_session.execute(
                select(Transition).where(Transition.entity_id == proposed_strategy.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].from_state == StrategyState.PROPOSED.value
    assert rows[0].to_state == StrategyState.BACKTESTED.value
    assert rows[0].entity_type == "strategy"
    assert rows[0].reason == "initial backtest submitted"


def _seed_index_criteria(settings) -> None:
    """Seed a permissive INDEX criteria YAML. check_backtest_criteria is now
    fail-closed on a missing YAML, so tests that drive backtested → paper must
    provide criteria the metrics can satisfy."""
    settings.criteria_dir.mkdir(parents=True, exist_ok=True)
    (settings.criteria_dir / "INDEX.yaml").write_text(
        yaml.safe_dump(
            {
                "backtest_to_paper": {"required_all": [{"sharpe": ">= 1.5"}]},
                "paper_to_live": {},
            }
        )
    )


async def test_strategy_full_happy_path(db_session, proposed_strategy):
    """proposed → backtested → paper_trading → live_trading."""
    from fwbg_agents.config import settings

    _seed_index_criteria(settings)
    await transition_strategy(
        db_session,
        proposed_strategy,
        StrategyState.BACKTESTED,
        reason="backtest done",
    )
    # backtested → paper_trading needs metrics that satisfy criteria. Provide
    # a metrics dict that beats the conservative defaults (sharpe>=1.5 etc).
    await transition_strategy(
        db_session,
        proposed_strategy,
        StrategyState.PAPER_TRADING,
        reason="metrics meet criteria",
        payload={
            "backtest_metrics": {
                "sharpe": 1.8,
                "mc_pvalue": 0.02,
                "profit_factor": 1.7,
                "min_trades": 350,
                "max_drawdown": 0.18,
            }
        },
    )
    await transition_strategy(
        db_session,
        proposed_strategy,
        StrategyState.LIVE_TRADING,
        reason="paper passed, human approved",
        payload={"human_approval": True},
    )
    assert proposed_strategy.current_state == StrategyState.LIVE_TRADING.value

    rows = (
        (
            await db_session.execute(
                select(Transition)
                .where(Transition.entity_id == proposed_strategy.id)
                .order_by(Transition.id)
            )
        )
        .scalars()
        .all()
    )
    assert [r.to_state for r in rows] == [
        StrategyState.BACKTESTED.value,
        StrategyState.PAPER_TRADING.value,
        StrategyState.LIVE_TRADING.value,
    ]


# ----- invalid transitions ---------------------------------------------------


async def test_strategy_cannot_skip_directly_to_live(db_session, proposed_strategy):
    with pytest.raises(InvalidTransitionError):
        await transition_strategy(
            db_session,
            proposed_strategy,
            StrategyState.LIVE_TRADING,
            reason="trying to skip",
            payload={"human_approval": True},
        )
    # Nothing was written.
    rows = (
        (
            await db_session.execute(
                select(Transition).where(Transition.entity_id == proposed_strategy.id)
            )
        )
        .scalars()
        .all()
    )
    assert rows == []
    assert proposed_strategy.current_state == StrategyState.PROPOSED.value


async def test_strategy_paper_to_live_requires_human_approval(db_session, proposed_strategy):
    from fwbg_agents.config import settings

    _seed_index_criteria(settings)
    await transition_strategy(db_session, proposed_strategy, StrategyState.BACKTESTED, reason="")
    await transition_strategy(
        db_session,
        proposed_strategy,
        StrategyState.PAPER_TRADING,
        reason="",
        payload={
            "backtest_metrics": {
                "sharpe": 1.8,
                "mc_pvalue": 0.02,
                "profit_factor": 1.7,
                "min_trades": 350,
                "max_drawdown": 0.18,
            }
        },
    )
    with pytest.raises(InvalidTransitionError) as exc:
        await transition_strategy(
            db_session,
            proposed_strategy,
            StrategyState.LIVE_TRADING,
            reason="auto-promote",
        )
    assert "human_approval" in str(exc.value).lower()


async def test_strategy_backtested_to_paper_rejects_failing_metrics(db_session, proposed_strategy):
    """Criteria YAML present, metrics fail sharpe gate."""
    from fwbg_agents.config import settings

    settings.criteria_dir.mkdir(parents=True, exist_ok=True)
    (settings.criteria_dir / "INDEX.yaml").write_text(
        yaml.safe_dump(
            {
                "backtest_to_paper": {
                    "required_all": [{"sharpe": ">= 1.5"}],
                },
                "paper_to_live": {},
            }
        )
    )

    await transition_strategy(db_session, proposed_strategy, StrategyState.BACKTESTED, reason="")
    with pytest.raises(InvalidTransitionError):
        await transition_strategy(
            db_session,
            proposed_strategy,
            StrategyState.PAPER_TRADING,
            reason="",
            payload={
                "backtest_metrics": {
                    "sharpe": 0.3,  # well below 1.5
                    "mc_pvalue": 0.02,
                    "profit_factor": 1.7,
                    "min_trades": 350,
                    "max_drawdown": 0.18,
                }
            },
        )


# ----- abandon ---------------------------------------------------------------


async def test_abandon_requires_post_mortem_path(db_session, proposed_strategy):
    with pytest.raises(InvalidTransitionError) as exc:
        await transition_strategy(
            db_session,
            proposed_strategy,
            StrategyState.ABANDONED,
            reason="no edge",
            payload={},  # missing post_mortem_path
        )
    assert "post_mortem_path" in str(exc.value)


async def test_abandon_persists_post_mortem_path(db_session, proposed_strategy, tmp_path):
    pm_path = strategy_dir("orb_dax_m15") / "post_mortem.yaml"
    await transition_strategy(
        db_session,
        proposed_strategy,
        StrategyState.ABANDONED,
        reason="no edge in any regime",
        payload={"post_mortem_path": str(pm_path)},
    )
    assert proposed_strategy.current_state == StrategyState.ABANDONED.value
    assert proposed_strategy.post_mortem_path == str(pm_path)
    # Lazy dir created at transition time.
    assert pm_path.parent.is_dir()


async def test_abandon_from_paper_trading_is_allowed(db_session, proposed_strategy):
    """Soft-abandon must be reachable from every non-terminal state."""
    from fwbg_agents.config import settings

    _seed_index_criteria(settings)
    await transition_strategy(db_session, proposed_strategy, StrategyState.BACKTESTED, reason="")
    await transition_strategy(
        db_session,
        proposed_strategy,
        StrategyState.PAPER_TRADING,
        reason="",
        payload={
            "backtest_metrics": {
                "sharpe": 1.8,
                "mc_pvalue": 0.02,
                "profit_factor": 1.7,
                "min_trades": 350,
                "max_drawdown": 0.18,
            }
        },
    )
    pm_path = strategy_dir("orb_dax_m15") / "post_mortem.yaml"
    await transition_strategy(
        db_session,
        proposed_strategy,
        StrategyState.ABANDONED,
        reason="drift in paper",
        payload={"post_mortem_path": str(pm_path)},
    )
    assert proposed_strategy.current_state == StrategyState.ABANDONED.value


async def test_cannot_leave_terminal_state(db_session, proposed_strategy):
    pm_path = strategy_dir("orb_dax_m15") / "post_mortem.yaml"
    await transition_strategy(
        db_session,
        proposed_strategy,
        StrategyState.ABANDONED,
        reason="",
        payload={"post_mortem_path": str(pm_path)},
    )
    with pytest.raises(InvalidTransitionError):
        await transition_strategy(
            db_session,
            proposed_strategy,
            StrategyState.BACKTESTED,
            reason="resurrect",
        )


# ----- filesystem -------------------------------------------------------------


async def test_strategy_dir_created_on_first_transition(db_session, proposed_strategy):
    """Directory is lazy — only materialised when first transition fires."""
    assert not strategy_dir("orb_dax_m15").exists()
    await transition_strategy(db_session, proposed_strategy, StrategyState.BACKTESTED, reason="")
    assert strategy_dir("orb_dax_m15").is_dir()


# ----- plugin ----------------------------------------------------------------


async def test_plugin_specified_to_authored(db_session, specified_plugin):
    await transition_plugin(
        db_session,
        specified_plugin,
        PluginState.AUTHORED,
        reason="code generated",
    )
    assert specified_plugin.current_state == PluginState.AUTHORED.value
    assert plugin_dir("atr_v2").is_dir()


async def test_plugin_invalid_skip(db_session, specified_plugin):
    with pytest.raises(InvalidTransitionError):
        await transition_plugin(
            db_session,
            specified_plugin,
            PluginState.ADOPTED_IN_FWBG,
            reason="ship it",
        )


async def test_plugin_abandon_requires_post_mortem(db_session, specified_plugin):
    with pytest.raises(InvalidTransitionError):
        await transition_plugin(
            db_session,
            specified_plugin,
            PluginState.ABANDONED,
            reason="contract too vague",
            payload={},
        )


# ----- criteria evaluator (unit) ---------------------------------------------


def test_eval_comparator_supports_basic_ops():
    from fwbg_agents.orchestrator.lifecycle import _eval_comparator

    assert _eval_comparator(">= 1.5", 1.5) is True
    assert _eval_comparator(">= 1.5", 1.4) is False
    assert _eval_comparator("<= 0.25", 0.20) is True
    assert _eval_comparator("<= 0.25", 0.26) is False
    assert _eval_comparator("< 0.05", 0.04) is True
    assert _eval_comparator("< 0.05", 0.05) is False
    assert _eval_comparator("> 1", 2) is True


def test_check_criteria_against_metrics_required_all(tmp_path, monkeypatch):
    """A real criteria YAML loaded from disk; ensure all gates are evaluated."""
    from fwbg_agents.config import settings
    from fwbg_agents.orchestrator.lifecycle import check_backtest_criteria

    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    settings.criteria_dir.mkdir(parents=True)
    (settings.criteria_dir / "INDEX.yaml").write_text(
        yaml.safe_dump(
            {
                "backtest_to_paper": {
                    "required_all": [
                        {"sharpe": ">= 1.5"},
                        {"mc_pvalue": "<= 0.05"},
                    ],
                    "hard_blockers": [
                        {"max_drawdown": "<= 0.25"},
                    ],
                },
                "paper_to_live": {},
            }
        )
    )
    ok, failed = check_backtest_criteria(
        asset_class="INDEX",
        metrics={"sharpe": 1.6, "mc_pvalue": 0.03, "max_drawdown": 0.20},
    )
    assert ok
    assert failed == []

    ok, failed = check_backtest_criteria(
        asset_class="INDEX",
        metrics={"sharpe": 1.0, "mc_pvalue": 0.03, "max_drawdown": 0.20},
    )
    assert not ok
    assert any("sharpe" in f for f in failed)


def test_check_criteria_section_evaluates_named_section(tmp_path, monkeypatch):
    """Plan 009 WP4: the promote gate evaluates its own criteria sections."""
    from fwbg_agents.config import settings
    from fwbg_agents.orchestrator.lifecycle import check_criteria_section

    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    settings.criteria_dir.mkdir(parents=True)
    (settings.criteria_dir / "FOREX.yaml").write_text(
        yaml.safe_dump(
            {
                "promote_holdout": {
                    "required_all": [{"annual_return": "> 0"}, {"sharpe": ">= 1.0"}]
                },
            }
        )
    )
    ok, failed = check_criteria_section(
        asset_class="FOREX",
        metrics={"annual_return": 12.0, "sharpe": 1.4},
        section="promote_holdout",
    )
    assert ok and failed == []

    ok, failed = check_criteria_section(
        asset_class="FOREX",
        metrics={"annual_return": -3.0, "sharpe": 1.4},
        section="promote_holdout",
    )
    assert not ok
    assert any("annual_return" in f for f in failed)


def test_check_criteria_section_fail_closed_on_missing_section(tmp_path, monkeypatch):
    from fwbg_agents.config import settings
    from fwbg_agents.orchestrator.lifecycle import check_criteria_section

    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    settings.criteria_dir.mkdir(parents=True)
    (settings.criteria_dir / "FOREX.yaml").write_text(yaml.safe_dump({"backtest_to_paper": {}}))
    # section absent → fail-closed
    ok, failed = check_criteria_section(
        asset_class="FOREX", metrics={"sharpe": 2.0}, section="promote_cost_stress"
    )
    assert not ok and failed
    # yaml absent entirely → fail-closed with calibrate hint
    ok, failed = check_criteria_section(asset_class="CRYPTO", metrics={}, section="promote_holdout")
    assert not ok and any("calibrate" in f for f in failed)


def test_missing_criteria_yaml_blocks_promotion(tmp_path, monkeypatch):
    """No criteria YAML for the asset class → the backtested → paper gate is
    CLOSED. A money-adjacent promotion must not pass unconditionally on a fresh
    checkout; POST /calibrate must seed criteria first.
    """
    from fwbg_agents.config import settings
    from fwbg_agents.orchestrator.lifecycle import check_backtest_criteria

    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    ok, failed = check_backtest_criteria(asset_class="CRYPTO", metrics={})
    assert not ok
    assert any("calibrate" in f for f in failed)


def test_non_numeric_metric_fails_cleanly(tmp_path, monkeypatch):
    """A malformed (non-numeric) metric yields a clean failure, not an
    unhandled ValueError from a bare float() cast."""
    from fwbg_agents.config import settings
    from fwbg_agents.orchestrator.lifecycle import check_backtest_criteria

    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    settings.criteria_dir.mkdir(parents=True)
    (settings.criteria_dir / "INDEX.yaml").write_text(
        yaml.safe_dump(
            {
                "backtest_to_paper": {"required_all": [{"sharpe": ">= 1.5"}]},
                "paper_to_live": {},
            }
        )
    )
    ok, failed = check_backtest_criteria(asset_class="INDEX", metrics={"sharpe": "n/a"})
    assert not ok
    assert any("not numeric" in f for f in failed)
