"""Translator agent — fresh mode (M4)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from fwbg_agents.agents import translator as translator_module
from fwbg_agents.agents.translator import Translator, TranslatorError
from fwbg_agents.orchestrator.hypotheses import ResearcherHypothesis, Source
from fwbg_agents.orchestrator.live_catalog import LiveCatalog
from fwbg_agents.orchestrator.plugin_catalog import PluginCatalog, PluginManifest
from fwbg_agents.persistence.database import Base
from fwbg_agents.persistence.models import (
    AgentRun,
    AgentRunStatus,
    Strategy,
    StrategyState,
)


def make_live_catalog(
    categories: dict[str, list[str]] | None = None,
    presets: dict[str, list[str]] | None = None,
) -> LiveCatalog:
    """Canned LiveCatalog for tests — replaces the live fwbg API fetch."""
    categories = categories if categories is not None else {
        "indicators": ["opening_range", "atr"],
        "models": ["signal_orb_v1"],
        "exit_strategies": ["orb_based", "atr_trailing_sl"],
    }
    by_category = {
        category: {
            slug: PluginManifest(
                name=slug, category=category, provenance="fwbg-core",
                version="1", source_path=".",
            )
            for slug in slugs
        }
        for category, slugs in categories.items()
    }
    details = {
        category: [{"name": slug, "description": "", "default_params": {}}
                   for slug in slugs]
        for category, slugs in categories.items()
    }
    return LiveCatalog(
        catalog=PluginCatalog(by_category=by_category),
        plugin_details=details,
        presets=presets or {},
    )


@pytest.fixture(autouse=True)
def canned_live_catalog(monkeypatch):
    """Hermetic tests: never hit the fwbg API or scan the real fwbg repo."""
    live = make_live_catalog()

    async def _fetch(_session, _client):
        return live

    monkeypatch.setattr(translator_module, "fetch_live_catalog", _fetch)
    return live


VALID_OUTPUT = {
    "name": "will_be_overwritten",
    "description": "ORB rule-based on FOREX majors",
    "hypothesis": "Opening range breakouts on EURUSD M15 produce a momentum edge.",
    "expected_outcome": "sharpe > 1.0 with PBO < 0.5",
    "datasource": "forexsb",
    "pipeline": {
        "indicators": [
            {"name": "opening_range", "params": {"range_bars": [1, 2, 4]}},
            {"name": "atr", "params": {"period": 14}},
        ],
    },
    "model": {
        "type": "signal_orb_v1",
        "architecture": "unified",
        "trade_directions": ["long", "short"],
        "hyperparameters": {},
    },
    "filters": {"min_trades": 50, "min_sharpe": 0.5},
    "validation": "walk_forward_intraday_v1",
    "resources": "standard_v1",
    "timeframe": "MINUTE_15",
    "exit_strategies": [
        {"name": "orb_based",
         "params": {"sl_mult": 1.0, "tp_mult": 5.0, "atr_period": 14},
         "ct": [0.5]},
    ],
    "tags": ["orb", "intraday", "forex_majors"],
    "optimization": {},
}


def _stub_model(output: dict) -> FunctionModel:
    def handler(_messages, _info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[ToolCallPart("final_result", output)])

    return FunctionModel(handler)


@pytest_asyncio.fixture
async def db_with_strategy(tmp_path, monkeypatch):
    from fwbg_agents.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path / "agents_data")

    db_url = f"sqlite+aiosqlite:///{tmp_path}/translator.db"
    engine = create_async_engine(db_url, future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    slug = "orb__forex__001"
    async with Session() as setup:
        now = datetime.now(UTC)
        s = Strategy(
            slug=slug,
            current_state=StrategyState.PROPOSED.value,
            iteration_count=1,
            asset_class="FOREX",
            strategy_family="ORB",
            created_at=now,
            updated_at=now,
        )
        setup.add(s)
        await setup.commit()
        await setup.refresh(s)
        strategy_id = s.id

    it_dir = settings.data_dir / "strategies" / slug / "iteration_001"
    it_dir.mkdir(parents=True, exist_ok=True)
    hyp = ResearcherHypothesis(
        title="ORB on FOREX majors",
        asset_class="FOREX",
        strategy_family="ORB",
        hypothesis="OR breakouts on EURUSD M15 have a session-driven edge.",
        expected_edge_explanation="Liquidity formation in early London creates persistence.",
        key_indicators=["opening_range", "atr"],
        tags=["orb", "intraday", "forex_majors"],
        sources=[Source(url="https://x", title="x", why_relevant="x")],
        differentiates_from=[],
    )
    (it_dir / "hypothesis.json").write_text(hyp.model_dump_json(indent=2))

    yield Session, strategy_id, slug, it_dir
    await engine.dispose()


@pytest.mark.asyncio
async def test_fresh_writes_strategy_json_and_spec_md(db_with_strategy):
    SessionMaker, strategy_id, slug, it_dir = db_with_strategy
    async with SessionMaker() as session:
        s = (await session.execute(select(Strategy).where(Strategy.id == strategy_id))).scalar_one()
        translator = Translator(session, model=_stub_model(VALID_OUTPUT))
        result_path = await translator.run_fresh(s)

    assert result_path == it_dir / "strategy.json"
    assert result_path.is_file()
    data = json.loads(result_path.read_text())
    assert data["name"] == slug  # overwritten with canonical slug
    assert data["pipeline"]["indicators"][0]["name"] == "opening_range"
    assert data["model"]["type"] == "signal_orb_v1"

    spec_md = it_dir / "spec.md"
    assert spec_md.is_file()
    spec_text = spec_md.read_text()
    assert "Goal" in spec_text and "Acceptance Criteria" in spec_text

    async with SessionMaker() as v:
        runs = (await v.execute(select(AgentRun))).scalars().all()
        assert len(runs) == 1
        assert runs[0].status == AgentRunStatus.DONE.value
        assert runs[0].agent_name == "translator"
        # spec_path set
        s2 = (await v.execute(select(Strategy).where(Strategy.id == strategy_id))).scalar_one()
        assert s2.spec_path == str(spec_md)


@pytest.mark.asyncio
async def test_fresh_invalid_structure_fails_translator_run(db_with_strategy):
    SessionMaker, strategy_id, *_ = db_with_strategy
    bad = dict(VALID_OUTPUT)
    bad.pop("exit_strategies")
    async with SessionMaker() as session:
        s = (await session.execute(select(Strategy).where(Strategy.id == strategy_id))).scalar_one()
        translator = Translator(session, model=_stub_model(bad))
        with pytest.raises(TranslatorError):
            await translator.run_fresh(s)

    async with SessionMaker() as v:
        runs = (await v.execute(select(AgentRun))).scalars().all()
        assert runs[0].status == AgentRunStatus.FAILED.value


@pytest.mark.asyncio
async def test_fresh_unknown_plugin_slug_fails(db_with_strategy):
    SessionMaker, strategy_id, *_ = db_with_strategy
    bad = dict(VALID_OUTPUT)
    bad["pipeline"] = {
        "indicators": [{"name": "totally_made_up_indicator_v77", "params": {}}]
    }
    async with SessionMaker() as session:
        s = (await session.execute(select(Strategy).where(Strategy.id == strategy_id))).scalar_one()
        translator = Translator(session, model=_stub_model(bad))
        with pytest.raises(TranslatorError):
            await translator.run_fresh(s)


@pytest.mark.asyncio
async def test_fresh_legacy_preset_string_pipeline_still_validates(db_with_strategy):
    """Pre-M7 payloads reference presets by name — still accepted, checked
    against the workspace preset list from the live catalog."""
    SessionMaker, strategy_id, *_ = db_with_strategy
    legacy = dict(VALID_OUTPUT)
    legacy["pipeline"] = "orb_simple_v1"
    legacy["model"] = "signal_orb_v1"
    legacy["filters"] = "orb_scalping_v1"
    async with SessionMaker() as session:
        s = (await session.execute(select(Strategy).where(Strategy.id == strategy_id))).scalar_one()
        translator = Translator(session, model=_stub_model(legacy))
        result_path = await translator.run_fresh(s)
    assert json.loads(result_path.read_text())["pipeline"] == "orb_simple_v1"


@pytest.mark.asyncio
async def test_fresh_missing_hypothesis_file_fails(db_with_strategy):
    SessionMaker, strategy_id, _slug, it_dir = db_with_strategy
    (it_dir / "hypothesis.json").unlink()
    async with SessionMaker() as session:
        s = (await session.execute(select(Strategy).where(Strategy.id == strategy_id))).scalar_one()
        translator = Translator(session, model=_stub_model(VALID_OUTPUT))
        with pytest.raises(FileNotFoundError):
            await translator.run_fresh(s)

    async with SessionMaker() as v:
        runs = (await v.execute(select(AgentRun))).scalars().all()
        assert runs[0].status == AgentRunStatus.FAILED.value
