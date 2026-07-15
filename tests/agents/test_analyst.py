"""Analyst agent tests.

The Analyst is the first LLM consumer in fwbg-agents. We use pydantic-ai's
TestModel to inject canned structured responses — no real LLM is called.

Recommendation variants tested:
- Promote
- Abandon (must include post_mortem_summary + lessons)
- TuneParams
- ChangeExit

Plus: missing fwbg_results.json → AgentRun status=failed + exception bubbles.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
import pytest_asyncio
import yaml
from pydantic_ai.messages import ModelRequest, ModelResponse, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from fwbg_agents.agents.analyst import (
    Abandon,
    AddIndicator,
    Analyst,
    ChangeExit,
    Promote,
    TuneParams,
    _best_symbol_metrics_from_results,
    _median_metrics_across_assets,
    _render_catalog_snapshot,
)
from fwbg_agents.orchestrator.plugin_catalog import (
    PluginCatalog,
    PluginManifest,
)
from fwbg_agents.persistence.database import Base
from fwbg_agents.persistence.models import (
    AgentRun,
    AgentRunStatus,
    LlmCall,
    Strategy,
    StrategyState,
)


def _stub_model(tool_name: str, args: dict) -> FunctionModel:
    """Return a FunctionModel that always emits one final-result tool call."""

    def handler(_messages, _info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[ToolCallPart(tool_name, args)])

    return FunctionModel(handler)


@pytest.fixture(autouse=True)
def _api_only_catalog(patch_live_catalog):
    """The catalog is API-only; the Analyst tests don't wire a FwbgClient, so
    use the shared DB-only fetch_live_catalog stub (see conftest)."""


@pytest_asyncio.fixture
async def db_and_backtested(tmp_path, monkeypatch):
    from fwbg_agents.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path / "agents_data")

    db_url = f"sqlite+aiosqlite:///{tmp_path}/analyst.db"
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
        await setup.refresh(s)
        strategy_id = s.id

    it_dir = settings.data_dir / "strategies" / "demo_v1" / "iteration_001"
    it_dir.mkdir(parents=True, exist_ok=True)
    (it_dir / "strategy.json").write_text(json.dumps({"name": "demo_v1"}))
    (it_dir / "fwbg_results.json").write_text(
        json.dumps(
            {
                "run_id": "abc",
                "status": "completed",
                "assets": {
                    "EURUSD": {
                        "unified_metrics": {
                            "sharpe": 1.8,
                            "profit_factor": 1.7,
                            "trades": 400,
                            "mc_pvalue": 0.03,
                            "max_drawdown": 0.18,
                        }
                    }
                },
            }
        )
    )

    yield Session, strategy_id, tmp_path
    await engine.dispose()


async def test_analyst_returns_promote(db_and_backtested):
    SessionMaker, strategy_id, _ = db_and_backtested
    test_model = _stub_model(
        "final_result_Promote",
        {
            "kind": "promote",
            "confidence": 0.9,
            "reasoning": "metrics clear the criteria across the board",
        },
    )
    async with SessionMaker() as session:
        s = (await session.execute(select(Strategy).where(Strategy.id == strategy_id))).scalar_one()
        analyst = Analyst(session, model=test_model)
        rec = await analyst.analyze(s)

    assert isinstance(rec, Promote)
    assert rec.confidence == 0.9
    assert "criteria" in rec.reasoning

    async with SessionMaker() as v:
        ar = (await v.execute(select(AgentRun))).scalars().all()
        assert len(ar) == 1
        assert ar[0].status == AgentRunStatus.DONE.value
        assert ar[0].agent_name == "analyst"
        calls = (await v.execute(select(LlmCall))).scalars().all()
        assert len(calls) == 1
        assert calls[0].agent_run_id == ar[0].id
        assert calls[0].input_tokens >= 0  # FunctionModel may report 0


async def test_analyst_returns_abandon(db_and_backtested):
    SessionMaker, strategy_id, _ = db_and_backtested
    test_model = _stub_model(
        "final_result_Abandon",
        {
            "kind": "abandon",
            "confidence": 0.8,
            "reasoning": "fundamentally unprofitable in this regime",
            "post_mortem_summary": "no edge after costs",
            "lessons": ["RSI mean-reversion on intraday DAX has no statistical edge"],
        },
    )
    async with SessionMaker() as session:
        s = (await session.execute(select(Strategy).where(Strategy.id == strategy_id))).scalar_one()
        analyst = Analyst(session, model=test_model)
        rec = await analyst.analyze(s)

    assert isinstance(rec, Abandon)
    assert "no edge" in rec.post_mortem_summary
    assert len(rec.lessons) == 1


async def test_analyst_returns_tune_params(db_and_backtested):
    SessionMaker, strategy_id, _ = db_and_backtested
    test_model = _stub_model(
        "final_result_TuneParams",
        {
            "kind": "tune_params",
            "confidence": 0.6,
            "reasoning": "tp too tight on M15",
            "param": "tp_multiplier",
            "new_range": [1.5, 2.0, 2.5],
        },
    )
    async with SessionMaker() as session:
        s = (await session.execute(select(Strategy).where(Strategy.id == strategy_id))).scalar_one()
        rec = await Analyst(session, model=test_model).analyze(s)

    # Legacy single-param shape is coerced into the params list.
    assert isinstance(rec, TuneParams)
    assert rec.params[0].param == "tp_multiplier"
    assert rec.params[0].new_range == [1.5, 2.0, 2.5]


async def test_analyst_returns_multi_tune_params(db_and_backtested):
    SessionMaker, strategy_id, _ = db_and_backtested
    test_model = _stub_model(
        "final_result_TuneParams",
        {
            "kind": "tune_params",
            "confidence": 0.6,
            "reasoning": "tp and sl both off",
            "params": [
                {"param": "tp_multiplier", "new_range": [1.5, 2.0, 2.5]},
                {"param": "sl_multiplier", "new_range": [0.5, 1.0]},
            ],
            "target_assets": ["EURUSD"],
        },
    )
    async with SessionMaker() as session:
        s = (await session.execute(select(Strategy).where(Strategy.id == strategy_id))).scalar_one()
        rec = await Analyst(session, model=test_model).analyze(s)

    assert isinstance(rec, TuneParams)
    assert [p.param for p in rec.params] == ["tp_multiplier", "sl_multiplier"]
    assert rec.target_assets == ["EURUSD"]


async def test_analyst_returns_change_exit(db_and_backtested):
    SessionMaker, strategy_id, _ = db_and_backtested
    test_model = _stub_model(
        "final_result_ChangeExit",
        {
            "kind": "change_exit",
            "confidence": 0.5,
            "reasoning": "static SL gets stopped on noise",
            "from_exit": "static_sl",
            "to_exit": "atr_trailing_sl",
            "new_exit_strategy": {"name": "atr_trailing_sl", "params": {"atr_mult": 2.0}},
        },
    )
    async with SessionMaker() as session:
        s = (await session.execute(select(Strategy).where(Strategy.id == strategy_id))).scalar_one()
        rec = await Analyst(session, model=test_model).analyze(s)

    assert isinstance(rec, ChangeExit)
    assert rec.from_exit == "static_sl"
    assert rec.to_exit == "atr_trailing_sl"
    assert rec.new_exit_strategy == {"name": "atr_trailing_sl", "params": {"atr_mult": 2.0}}


async def test_analyst_returns_add_indicator(db_and_backtested, monkeypatch, tmp_path):
    SessionMaker, strategy_id, _ = db_and_backtested

    test_model = _stub_model(
        "final_result_AddIndicator",
        {
            "kind": "add_indicator",
            "confidence": 0.7,
            "reasoning": "no zone-pivot plugin in catalog; strategy needs support/resistance bands",
            "phase": "indicators",
            "capability": "support/resistance zones from pivot points",
            "category": "indicator",
        },
    )
    async with SessionMaker() as session:
        s = (await session.execute(select(Strategy).where(Strategy.id == strategy_id))).scalar_one()
        rec = await Analyst(session, model=test_model).analyze(s)

    assert isinstance(rec, AddIndicator)
    assert rec.phase == "indicators"
    assert rec.category == "indicator"
    assert "pivot" in rec.capability


def test_render_catalog_snapshot_lists_categories_and_slugs():
    cat = PluginCatalog(
        by_category={
            "indicators": {
                slug: PluginManifest(
                    name=slug,
                    category="indicators",
                    provenance="fwbg-core",
                    version="1.0.0",
                    source_path=Path("/tmp/x"),
                )
                for slug in ["ema", "sma"]
            },
            "exit_strategies": {
                "fixed": PluginManifest(
                    name="fixed",
                    category="exit_strategies",
                    provenance="fwbg-core",
                    version="1.0.0",
                    source_path=Path("/tmp/x"),
                ),
            },
        }
    )
    snap = _render_catalog_snapshot(cat)
    # Category labels are normalised to the AddIndicator.category enum spelling
    # (singular) so the model copies a valid category token.
    assert "indicator: ema, sma" in snap
    assert "exit_strategy: fixed" in snap
    assert "indicators:" not in snap  # plural key must not leak into the snapshot


def test_render_catalog_snapshot_empty():
    snap = _render_catalog_snapshot(PluginCatalog(by_category={}))
    assert "catalog empty" in snap.lower()


def test_add_indicator_coerces_plural_category_and_bad_phase():
    """The model copies plural category keys ('indicators') from the snapshot and
    invents phases the prompt never lists ('entry'). Both used to exhaust
    pydantic-ai's output retries and crash the analyst — the before-validator now
    degrades them to the closest valid enum member instead."""
    rec = AddIndicator(
        confidence=0.7,
        reasoning="needs pivot zones",
        phase="entry",
        capability="support/resistance zones",
        category="indicators",
    )
    assert rec.category == "indicator"
    assert rec.phase == "indicators"


async def test_analyst_add_indicator_survives_invalid_enums(
    db_and_backtested, monkeypatch, tmp_path
):
    """End-to-end regression for the production crash: a model emitting the
    plural category + bogus phase must yield a normalised AddIndicator and a
    DONE AgentRun rather than 'Exceeded maximum output retries (3)'."""
    SessionMaker, strategy_id, _ = db_and_backtested

    test_model = _stub_model(
        "final_result_AddIndicator",
        {
            "kind": "add_indicator",
            "confidence": 0.7,
            "reasoning": "no pivot-zone plugin in catalog",
            "phase": "entry",  # not a valid phase — model hallucination
            "capability": "support/resistance zones from pivot points",
            "category": "indicators",  # plural — copied from the catalog snapshot
        },
    )
    async with SessionMaker() as session:
        s = (await session.execute(select(Strategy).where(Strategy.id == strategy_id))).scalar_one()
        rec = await Analyst(session, model=test_model).analyze(s)

    assert isinstance(rec, AddIndicator)
    assert rec.category == "indicator"
    assert rec.phase == "indicators"

    async with SessionMaker() as v:
        ar = (await v.execute(select(AgentRun))).scalars().all()
        assert len(ar) == 1
        assert ar[0].status == AgentRunStatus.DONE.value


async def test_analyst_missing_results_marks_agent_run_failed(db_and_backtested):
    SessionMaker, strategy_id, _ = db_and_backtested
    from fwbg_agents.config import settings as cfg

    results = cfg.data_dir / "strategies" / "demo_v1" / "iteration_001" / "fwbg_results.json"
    results.unlink()

    test_model = _stub_model(
        "final_result_Promote", {"kind": "promote", "confidence": 1.0, "reasoning": "x"}
    )
    async with SessionMaker() as session:
        s = (await session.execute(select(Strategy).where(Strategy.id == strategy_id))).scalar_one()
        with pytest.raises(FileNotFoundError):
            await Analyst(session, model=test_model).analyze(s)

    async with SessionMaker() as v:
        ar = (await v.execute(select(AgentRun))).scalars().all()
        assert len(ar) == 1
        assert ar[0].status == AgentRunStatus.FAILED.value


# --- WP2: median-across-universe gate --------------------------------------


def test_median_metrics_across_assets_takes_per_metric_median():
    run = {
        "assets": {
            "EURUSD": {"unified_metrics": {"sharpe": 2.0, "trades": 400}},
            "GBPUSD": {"unified_metrics": {"sharpe": 1.0, "trades": 200}},
            "USDJPY": {"unified_metrics": {"sharpe": 0.0, "trades": 600}},
        }
    }
    med = _median_metrics_across_assets(run)
    assert med == {"sharpe": 1.0, "trades": 400.0}


def test_median_metrics_single_asset_equals_that_asset():
    """A single-asset universe is unaffected — the median is that asset."""
    run = {"assets": {"EURUSD": {"unified_metrics": {"sharpe": 1.8, "trades": 400}}}}
    assert _median_metrics_across_assets(run) == {"sharpe": 1.8, "trades": 400.0}


def test_median_metrics_ignores_assets_without_metrics():
    assert _median_metrics_across_assets({"assets": {}}) == {}
    assert _median_metrics_across_assets({}) == {}
    run = {"assets": {"EURUSD": {"status": "error", "unified_metrics": {}}}}
    assert _median_metrics_across_assets(run) == {}


def _seed_forex_criteria(tmp_path, monkeypatch):
    from fwbg_agents.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    settings.criteria_dir.mkdir(parents=True)
    (settings.criteria_dir / "FOREX.yaml").write_text(
        yaml.safe_dump(
            {
                "backtest_to_paper": {"required_all": [{"sharpe": ">= 1.5"}]},
                "paper_to_live": {},
            }
        )
    )


def test_median_gate_rejects_lone_star(tmp_path, monkeypatch):
    """One stellar asset must not carry the gate — the median decides."""
    from fwbg_agents.orchestrator.lifecycle import check_backtest_criteria

    _seed_forex_criteria(tmp_path, monkeypatch)
    run = {
        "assets": {
            "EURUSD": {"unified_metrics": {"sharpe": 3.0}},
            "GBPUSD": {"unified_metrics": {"sharpe": 0.5}},
            "USDJPY": {"unified_metrics": {"sharpe": 0.4}},
            "AUDUSD": {"unified_metrics": {"sharpe": 0.6}},
            "USDCHF": {"unified_metrics": {"sharpe": 0.3}},
        }
    }
    # Best symbol (3.0) would have passed the old gate.
    best = _best_symbol_metrics_from_results(run)
    assert best["sharpe"] == 3.0
    ok_best, _ = check_backtest_criteria(asset_class="FOREX", metrics=best)
    assert ok_best
    # The median (0.5) fails — the strategy is not broadly profitable.
    med = _median_metrics_across_assets(run)
    assert med["sharpe"] == 0.5
    ok_med, failed = check_backtest_criteria(asset_class="FOREX", metrics=med)
    assert not ok_med
    assert any("sharpe" in f for f in failed)


def test_median_gate_passes_homogeneous_and_single(tmp_path, monkeypatch):
    from fwbg_agents.orchestrator.lifecycle import check_backtest_criteria

    _seed_forex_criteria(tmp_path, monkeypatch)
    homogeneous = {
        "assets": {
            "EURUSD": {"unified_metrics": {"sharpe": 1.6}},
            "GBPUSD": {"unified_metrics": {"sharpe": 1.7}},
            "USDJPY": {"unified_metrics": {"sharpe": 2.0}},
        }
    }
    ok, failed = check_backtest_criteria(
        asset_class="FOREX", metrics=_median_metrics_across_assets(homogeneous)
    )
    assert ok and failed == []

    single = {"assets": {"EURUSD": {"unified_metrics": {"sharpe": 1.8}}}}
    ok, failed = check_backtest_criteria(
        asset_class="FOREX", metrics=_median_metrics_across_assets(single)
    )
    assert ok and failed == []


async def test_analyst_prompt_surfaces_median_metrics(db_and_backtested):
    """The rendered prompt must present the median section (the gated one)."""
    SessionMaker, strategy_id, _ = db_and_backtested
    captured: dict[str, str] = {}

    def handler(messages, _info: AgentInfo) -> ModelResponse:
        texts: list[str] = []
        for m in messages:
            for p in getattr(m, "parts", []):
                c = getattr(p, "content", None)
                if isinstance(c, str):
                    texts.append(c)
        captured["prompt"] = "\n".join(texts)
        return ModelResponse(
            parts=[
                ToolCallPart(
                    "final_result_Promote",
                    {"kind": "promote", "confidence": 0.5, "reasoning": "x"},
                )
            ]
        )

    async with SessionMaker() as session:
        s = (await session.execute(select(Strategy).where(Strategy.id == strategy_id))).scalar_one()
        await Analyst(session, model=FunctionModel(handler)).analyze(s)

    assert "MEDIAN across the universe" in captured["prompt"]
    # Single-asset fixture → median equals that asset's sharpe (1.8).
    assert '"sharpe": 1.8' in captured["prompt"]


async def test_analyst_prompt_includes_trade_diagnostics_sidecar(db_and_backtested):
    """A trade_diagnostics.md sidecar is injected into the prompt verbatim."""
    from fwbg_agents.config import settings

    SessionMaker, strategy_id, _ = db_and_backtested
    sidecar = (
        settings.data_dir / "strategies" / "demo_v1" / "iteration_001" / "trade_diagnostics.md"
    )
    sidecar.write_text("### All assets — 42 trades\n- payoff ratio: 0.31")

    captured: dict[str, str] = {}

    def handler(messages, _info: AgentInfo) -> ModelResponse:
        texts: list[str] = []
        for m in messages:
            for p in getattr(m, "parts", []):
                c = getattr(p, "content", None)
                if isinstance(c, str):
                    texts.append(c)
        captured["prompt"] = "\n".join(texts)
        return ModelResponse(
            parts=[
                ToolCallPart(
                    "final_result_Promote",
                    {"kind": "promote", "confidence": 0.5, "reasoning": "x"},
                )
            ]
        )

    async with SessionMaker() as session:
        s = (await session.execute(select(Strategy).where(Strategy.id == strategy_id))).scalar_one()
        await Analyst(session, model=FunctionModel(handler)).analyze(s)

    assert "## Trade-Diagnostik" in captured["prompt"]
    assert "payoff ratio: 0.31" in captured["prompt"]


# --- Analyst tool-use over per-trade data (Plan 010 WP4) --------------------


def _write_trade_fold_results(settings, run_id: str, symbol: str, trades: list[dict]) -> None:
    sym_dir = settings.fwbg_test_results_dir / run_id / "grid_details" / symbol
    sym_dir.mkdir(parents=True, exist_ok=True)
    (sym_dir / "fold_results.json").write_text(
        json.dumps({"walk_forward": {"fold_details": [{"test_trades_detail": trades}]}})
    )


async def test_analyst_query_trades_tool_returns_plausible_result(db_and_backtested, monkeypatch):
    """A tool-call round trip: the model queries trades, gets back real rows,
    then emits its final recommendation."""
    from fwbg_agents.config import settings

    SessionMaker, strategy_id, tmp_path = db_and_backtested
    monkeypatch.setattr(settings, "fwbg_test_results_dir", tmp_path / "test_results")
    _write_trade_fold_results(
        settings,
        "abc",
        "EURUSD",
        [
            {"pnl_raw": 10.0, "entry_time": "2025-01-01T10:00:00", "hour": 10},
            {"pnl_raw": -5.0, "entry_time": "2025-01-01T11:00:00", "hour": 11},
        ],
    )

    captured: dict[str, str] = {}

    def handler(messages: list[ModelRequest], _info: AgentInfo) -> ModelResponse:
        seen_query = any(
            isinstance(part, ToolReturnPart) and part.tool_name == "query_trades_tool"
            for msg in messages
            for part in getattr(msg, "parts", [])
        )
        if not seen_query:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "query_trades_tool",
                        {"sql": "SELECT hour, pnl_raw FROM trades ORDER BY hour"},
                    )
                ]
            )
        for msg in messages:
            for part in getattr(msg, "parts", []):
                if isinstance(part, ToolReturnPart) and part.tool_name == "query_trades_tool":
                    captured["tool_result"] = part.content
        return ModelResponse(
            parts=[
                ToolCallPart(
                    "final_result_Promote",
                    {"kind": "promote", "confidence": 0.7, "reasoning": "queried trades"},
                )
            ]
        )

    async with SessionMaker() as session:
        s = (await session.execute(select(Strategy).where(Strategy.id == strategy_id))).scalar_one()
        rec = await Analyst(session, model=FunctionModel(handler)).analyze(s)

    assert isinstance(rec, Promote)
    rows = json.loads(captured["tool_result"])
    assert rows == [{"hour": 10, "pnl_raw": 10.0}, {"hour": 11, "pnl_raw": -5.0}]


async def test_analyst_query_trades_tool_rejects_unsafe_sql(db_and_backtested, monkeypatch):
    from fwbg_agents.config import settings

    SessionMaker, strategy_id, tmp_path = db_and_backtested
    monkeypatch.setattr(settings, "fwbg_test_results_dir", tmp_path / "test_results")
    _write_trade_fold_results(settings, "abc", "EURUSD", [{"pnl_raw": 1.0}])

    captured: dict[str, str] = {}

    def handler(messages: list[ModelRequest], _info: AgentInfo) -> ModelResponse:
        seen_query = any(
            isinstance(part, ToolReturnPart) and part.tool_name == "query_trades_tool"
            for msg in messages
            for part in getattr(msg, "parts", [])
        )
        if not seen_query:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "query_trades_tool",
                        {"sql": "SELECT * FROM trades; DROP TABLE trades"},
                    )
                ]
            )
        for msg in messages:
            for part in getattr(msg, "parts", []):
                if isinstance(part, ToolReturnPart) and part.tool_name == "query_trades_tool":
                    captured["tool_result"] = part.content
        return ModelResponse(
            parts=[
                ToolCallPart(
                    "final_result_Promote",
                    {"kind": "promote", "confidence": 0.5, "reasoning": "x"},
                )
            ]
        )

    async with SessionMaker() as session:
        s = (await session.execute(select(Strategy).where(Strategy.id == strategy_id))).scalar_one()
        await Analyst(session, model=FunctionModel(handler)).analyze(s)

    assert captured["tool_result"].startswith("query rejected:")


async def test_analyst_query_trades_tool_enforces_row_cap(db_and_backtested, monkeypatch):
    from fwbg_agents.config import settings
    from fwbg_agents.orchestrator.trade_diagnostics import TRADE_QUERY_ROW_CAP

    SessionMaker, strategy_id, tmp_path = db_and_backtested
    monkeypatch.setattr(settings, "fwbg_test_results_dir", tmp_path / "test_results")
    trades = [{"pnl_raw": float(i)} for i in range(TRADE_QUERY_ROW_CAP + 20)]
    _write_trade_fold_results(settings, "abc", "EURUSD", trades)

    captured: dict[str, str] = {}

    def handler(messages: list[ModelRequest], _info: AgentInfo) -> ModelResponse:
        seen_query = any(
            isinstance(part, ToolReturnPart) and part.tool_name == "query_trades_tool"
            for msg in messages
            for part in getattr(msg, "parts", [])
        )
        if not seen_query:
            return ModelResponse(
                parts=[ToolCallPart("query_trades_tool", {"sql": "SELECT pnl_raw FROM trades"})]
            )
        for msg in messages:
            for part in getattr(msg, "parts", []):
                if isinstance(part, ToolReturnPart) and part.tool_name == "query_trades_tool":
                    captured["tool_result"] = part.content
        return ModelResponse(
            parts=[
                ToolCallPart(
                    "final_result_Promote",
                    {"kind": "promote", "confidence": 0.5, "reasoning": "x"},
                )
            ]
        )

    async with SessionMaker() as session:
        s = (await session.execute(select(Strategy).where(Strategy.id == strategy_id))).scalar_one()
        await Analyst(session, model=FunctionModel(handler)).analyze(s)

    rows = json.loads(captured["tool_result"])
    assert len(rows) == TRADE_QUERY_ROW_CAP


async def test_analyst_describe_trades_tool_reports_schema(db_and_backtested, monkeypatch):
    from fwbg_agents.config import settings

    SessionMaker, strategy_id, tmp_path = db_and_backtested
    monkeypatch.setattr(settings, "fwbg_test_results_dir", tmp_path / "test_results")
    _write_trade_fold_results(
        settings, "abc", "EURUSD", [{"pnl_raw": 1.0, "entry_time": "2025-01-01T10:00:00"}]
    )

    def handler(messages: list[ModelRequest], _info: AgentInfo) -> ModelResponse:
        seen_describe = any(
            isinstance(part, ToolReturnPart) and part.tool_name == "describe_trades_tool"
            for msg in messages
            for part in getattr(msg, "parts", [])
        )
        if not seen_describe:
            return ModelResponse(parts=[ToolCallPart("describe_trades_tool", {})])
        return ModelResponse(
            parts=[
                ToolCallPart(
                    "final_result_Promote",
                    {"kind": "promote", "confidence": 0.5, "reasoning": "x"},
                )
            ]
        )

    async with SessionMaker() as session:
        s = (await session.execute(select(Strategy).where(Strategy.id == strategy_id))).scalar_one()
        await Analyst(session, model=FunctionModel(handler)).analyze(s)


async def test_analyst_emits_analyst_query_run_event(db_and_backtested, monkeypatch):
    from fwbg_agents.config import settings
    from fwbg_agents.run_events import run_dir

    SessionMaker, strategy_id, tmp_path = db_and_backtested
    monkeypatch.setattr(settings, "fwbg_test_results_dir", tmp_path / "test_results")
    _write_trade_fold_results(settings, "abc", "EURUSD", [{"pnl_raw": 1.0}])

    def handler(messages: list[ModelRequest], _info: AgentInfo) -> ModelResponse:
        seen_query = any(
            isinstance(part, ToolReturnPart) and part.tool_name == "query_trades_tool"
            for msg in messages
            for part in getattr(msg, "parts", [])
        )
        if not seen_query:
            return ModelResponse(
                parts=[ToolCallPart("query_trades_tool", {"sql": "SELECT pnl_raw FROM trades"})]
            )
        return ModelResponse(
            parts=[
                ToolCallPart(
                    "final_result_Promote",
                    {"kind": "promote", "confidence": 0.5, "reasoning": "x"},
                )
            ]
        )

    async with SessionMaker() as session:
        s = (await session.execute(select(Strategy).where(Strategy.id == strategy_id))).scalar_one()
        await Analyst(session, model=FunctionModel(handler)).analyze(s)

    async with SessionMaker() as v:
        ar = (await v.execute(select(AgentRun))).scalars().all()[0]
    events_path = run_dir(ar.id) / "events.jsonl"
    events = [json.loads(line) for line in events_path.read_text().splitlines()]
    assert any(
        e["type"] == "analyst_query" and e["sql"] == "SELECT pnl_raw FROM trades" for e in events
    )
