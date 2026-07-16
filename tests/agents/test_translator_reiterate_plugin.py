"""Translator.run_reiterate_with_plugin — M5c Task 2.

Deterministic splice: given a parent Strategy in BACKTESTED and a sidecar
dict identifying a VERIFIED plugin slug + phase, produce a child Strategy
in PROPOSED with the slug appended to the right list-field (`indicators` /
`feature_selection` / `preprocessing` / `extra_filters`).

The plugin must already be in the catalog (caller's responsibility): we
seed a Plugin row in VERIFIED so the merged catalog picks it up. The catalog
is API-only, so `fetch_live_catalog` is stubbed (via the shared
`patch_live_catalog` fixture) to a hermetic DB-only catalog — no live fwbg.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from fwbg_agents.agents.translator import Translator, TranslatorError
from fwbg_agents.persistence.database import Base
from fwbg_agents.persistence.models import (
    AgentRun,
    AgentRunStatus,
    Plugin,
    PluginState,
    Strategy,
    StrategyState,
    StrategyTag,
    Transition,
)

PARENT_STRATEGY_JSON = {
    "name": "orb__forex__001",
    "description": "ORB rule-based on FOREX majors",
    "hypothesis": "Opening range breakouts on EURUSD M15.",
    "expected_outcome": "sharpe > 1.0",
    "datasource": "forexsb",
    "pipeline": "orb_simple_v1",
    "model": "signal_orb_v1",
    "filters": "orb_scalping_v1",
    "validation": "walk_forward_intraday_v1",
    "resources": "standard_v1",
    "timeframe": "MINUTE_15",
    "exit_strategies": [
        {
            "name": "orb_based",
            "params": {"sl_mult": 1.0, "tp_mult": 5.0, "atr_period": 14},
            "ct": [0.5],
        },
    ],
    "tags": ["orb", "intraday", "forex_majors"],
    "optimization": {"grid_params": {"sl_mult": [0.9, 1.0, 1.1]}},
}


PARENT_HYPOTHESIS = {
    "title": "ORB on FOREX majors",
    "asset_class": "FOREX",
    "strategy_family": "ORB",
    "hypothesis": "OR breakouts on EURUSD M15.",
    "expected_edge_explanation": "Liquidity formation in early London.",
    "key_indicators": ["opening_range", "atr"],
    "tags": ["orb", "intraday", "forex_majors"],
    "sources": [{"url": "https://x", "title": "x", "why_relevant": "x"}],
    "differentiates_from": [],
}


PLUGIN_SLUG = "adx-trend-strength"


def _make_plugin(slug: str, kind: str) -> Plugin:
    now = datetime.now(UTC)
    return Plugin(
        slug=slug,
        current_state=PluginState.VERIFIED.value,
        kind=kind,
        spec_path=f"data/plugins/{slug}/v1/spec.md",
        contract_path=f"data/plugins/{slug}/v1/contract.yaml",
        created_at=now,
        updated_at=now,
    )


def _sidecar(
    phase: str, *, slug: str = PLUGIN_SLUG, capability: str = "detect strong trends"
) -> dict:
    return {
        "kind": "add_indicator",
        "capability": capability,
        "category": phase,
        "phase": phase,
        "confidence": 0.85,
        "reasoning": "Trend filter expected to reduce whipsaws.",
        "plugin_slug": slug,
    }


@pytest.fixture(autouse=True)
def _stub_catalog(patch_live_catalog):
    """Catalog is API-only; these tests don't wire a FwbgClient, so use the
    shared DB-only fetch_live_catalog stub (see conftest)."""


@pytest_asyncio.fixture
async def db_with_parent(tmp_path, monkeypatch):
    """Seed: parent in BACKTESTED + iteration_001 files on disk."""
    from fwbg_agents.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path / "agents_data")

    db_url = f"sqlite+aiosqlite:///{tmp_path}/translator_reiter_plugin.db"
    engine = create_async_engine(db_url, future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    parent_slug = "orb__forex__001"
    async with Session() as setup:
        now = datetime.now(UTC)
        s = Strategy(
            slug=parent_slug,
            current_state=StrategyState.BACKTESTED.value,
            iteration_count=1,
            asset_class="FOREX",
            strategy_family="ORB",
            created_at=now,
            updated_at=now,
        )
        setup.add(s)
        await setup.flush()
        for tag in PARENT_STRATEGY_JSON["tags"]:
            setup.add(StrategyTag(strategy_id=s.id, tag=tag))
        # Seed PLUGIN_SLUG as a VERIFIED `indicators` plugin so the
        # happy-path indicator test sees it in the catalog. Other phase
        # tests insert their own slug+kind via `_seed_plugin_kind`.
        setup.add(_make_plugin(PLUGIN_SLUG, "indicators"))
        await setup.commit()
        await setup.refresh(s)
        parent_id = s.id

    it_dir = settings.data_dir / "strategies" / parent_slug / "iteration_001"
    it_dir.mkdir(parents=True, exist_ok=True)
    (it_dir / "strategy.json").write_text(json.dumps(PARENT_STRATEGY_JSON, indent=2))
    (it_dir / "hypothesis.json").write_text(json.dumps(PARENT_HYPOTHESIS, indent=2))

    yield Session, parent_id, parent_slug, it_dir
    await engine.dispose()


async def _seed_plugin_kind(SessionMaker, slug: str, kind: str) -> None:
    """Helper: insert a VERIFIED plugin row of a given catalog `kind`.

    `slug` is globally unique on the Plugin table; callers must use a
    distinct slug per call OR ensure no prior row exists.
    """
    async with SessionMaker() as s:
        s.add(_make_plugin(slug, kind))
        await s.commit()


@pytest.mark.asyncio
async def test_run_reiterate_with_plugin_happy_path_indicator_phase(db_with_parent):
    SessionMaker, parent_id, parent_slug, _it_dir = db_with_parent
    # Default fixture seeds slug under kind=indicators (first iteration of loop)
    # — that's exactly what we want for this test.
    sidecar = _sidecar("indicators")

    async with SessionMaker() as session:
        parent = (
            await session.execute(select(Strategy).where(Strategy.id == parent_id))
        ).scalar_one()
        child = await Translator(session).run_reiterate_with_plugin(parent, PLUGIN_SLUG, sidecar)

    assert isinstance(child, Strategy)
    assert child.current_state == StrategyState.PROPOSED.value
    assert child.parent_strategy_id == parent_id
    assert child.iteration_count == 1
    assert child.slug != parent_slug

    from fwbg_agents.config import settings

    child_dir = settings.data_dir / "strategies" / child.slug / "iteration_001"
    strategy_path = child_dir / "strategy.json"
    hypothesis_path = child_dir / "hypothesis.json"
    spec_path = child_dir / "spec.md"
    assert strategy_path.is_file()
    assert hypothesis_path.is_file()
    assert spec_path.is_file()

    child_payload = json.loads(strategy_path.read_text())
    assert child_payload["indicators"] == [PLUGIN_SLUG]
    assert child_payload["name"] == child.slug

    hypothesis_data = json.loads(hypothesis_path.read_text())
    iterations = hypothesis_data["iterations"]
    assert iterations[-1]["plugin_slug"] == PLUGIN_SLUG
    assert PLUGIN_SLUG in iterations[-1]["rationale"]
    assert "indicator" in iterations[-1]["rationale"]
    assert "detect strong trends" in iterations[-1]["rationale"]

    async with SessionMaker() as v:
        ts = (
            (await v.execute(select(Transition).where(Transition.entity_id == child.id)))
            .scalars()
            .all()
        )
        assert len(ts) == 1
        assert ts[0].to_state == StrategyState.PROPOSED.value
        assert ts[0].payload["parent_strategy_id"] == parent_id
        assert ts[0].payload["plugin_slug"] == PLUGIN_SLUG
        assert ts[0].payload["sidecar"] == sidecar

        ars = (
            (await v.execute(select(AgentRun).where(AgentRun.agent_name == "translator")))
            .scalars()
            .all()
        )
        assert len(ars) == 1
        assert ars[0].status == AgentRunStatus.DONE.value


def test_phase_to_field_keys_are_sdk_plugin_phases():
    """The phase vocabulary is fwbg_sdk.PluginPhase — no other spellings."""
    from fwbg_sdk.base import PluginPhase

    from fwbg_agents.agents.translator import _PHASE_TO_FIELD

    assert set(_PHASE_TO_FIELD) <= {p.value for p in PluginPhase}


@pytest.mark.asyncio
async def test_run_reiterate_with_plugin_rejects_legacy_singular_phase(db_with_parent):
    # Pre-enum sidecars used singular spellings ("indicator"); those are no
    # longer part of the vocabulary and must be rejected.
    SessionMaker, parent_id, _parent_slug, _it_dir = db_with_parent
    sidecar = _sidecar("indicator")

    async with SessionMaker() as session:
        parent = (
            await session.execute(select(Strategy).where(Strategy.id == parent_id))
        ).scalar_one()
        with pytest.raises(TranslatorError, match="unknown phase"):
            await Translator(session).run_reiterate_with_plugin(parent, PLUGIN_SLUG, sidecar)


@pytest.mark.asyncio
async def test_run_reiterate_with_plugin_feature_selection_phase(db_with_parent):
    SessionMaker, parent_id, _parent_slug, _it_dir = db_with_parent
    # The fixture seeded slug under `indicators`. For this test we need it
    # under `feature_selection`. Easiest: use a different slug.
    slug = "boruta-selector"
    await _seed_plugin_kind(SessionMaker, slug, "feature_selection")
    sidecar = _sidecar("feature_selection", slug=slug, capability="select stable features")

    async with SessionMaker() as session:
        parent = (
            await session.execute(select(Strategy).where(Strategy.id == parent_id))
        ).scalar_one()
        child = await Translator(session).run_reiterate_with_plugin(parent, slug, sidecar)

    from fwbg_agents.config import settings

    child_payload = json.loads(
        (
            settings.data_dir / "strategies" / child.slug / "iteration_001" / "strategy.json"
        ).read_text()
    )
    assert child_payload["feature_selection"] == [slug]
    assert "indicators" not in child_payload or child_payload["indicators"] == []
    assert "preprocessing" not in child_payload or child_payload["preprocessing"] == []
    assert "extra_filters" not in child_payload or child_payload["extra_filters"] == []


@pytest.mark.asyncio
async def test_run_reiterate_with_plugin_preprocessing_phase(db_with_parent):
    SessionMaker, parent_id, _parent_slug, _it_dir = db_with_parent
    slug = "zscore-normalizer"
    await _seed_plugin_kind(SessionMaker, slug, "preprocessing")
    sidecar = _sidecar("preprocessing", slug=slug, capability="z-score input features")

    async with SessionMaker() as session:
        parent = (
            await session.execute(select(Strategy).where(Strategy.id == parent_id))
        ).scalar_one()
        child = await Translator(session).run_reiterate_with_plugin(parent, slug, sidecar)

    from fwbg_agents.config import settings

    child_payload = json.loads(
        (
            settings.data_dir / "strategies" / child.slug / "iteration_001" / "strategy.json"
        ).read_text()
    )
    assert child_payload["preprocessing"] == [slug]


@pytest.mark.asyncio
async def test_run_reiterate_with_plugin_filter_phase(db_with_parent):
    SessionMaker, parent_id, _parent_slug, _it_dir = db_with_parent
    slug = "volatility-regime-filter"
    await _seed_plugin_kind(SessionMaker, slug, "filters")
    sidecar = _sidecar("risk_management", slug=slug, capability="skip low-vol regimes")

    async with SessionMaker() as session:
        parent = (
            await session.execute(select(Strategy).where(Strategy.id == parent_id))
        ).scalar_one()
        child = await Translator(session).run_reiterate_with_plugin(parent, slug, sidecar)

    from fwbg_agents.config import settings

    child_payload = json.loads(
        (
            settings.data_dir / "strategies" / child.slug / "iteration_001" / "strategy.json"
        ).read_text()
    )
    assert child_payload["extra_filters"] == [slug]
    # Critically: parent's legacy `filters` single-string must be untouched.
    assert child_payload["filters"] == PARENT_STRATEGY_JSON["filters"]


@pytest.mark.asyncio
async def test_run_reiterate_with_plugin_rejects_unknown_phase(db_with_parent):
    SessionMaker, parent_id, _parent_slug, _it_dir = db_with_parent
    sidecar = _sidecar("orchestration")

    async with SessionMaker() as session:
        parent = (
            await session.execute(select(Strategy).where(Strategy.id == parent_id))
        ).scalar_one()
        with pytest.raises(TranslatorError) as exc:
            await Translator(session).run_reiterate_with_plugin(parent, PLUGIN_SLUG, sidecar)
    assert "orchestration" in str(exc.value)

    async with SessionMaker() as v:
        children = (
            (await v.execute(select(Strategy).where(Strategy.parent_strategy_id == parent_id)))
            .scalars()
            .all()
        )
        assert children == []

        ars = (
            (await v.execute(select(AgentRun).where(AgentRun.agent_name == "translator")))
            .scalars()
            .all()
        )
        assert len(ars) == 1
        assert ars[0].status == AgentRunStatus.FAILED.value
        assert "orchestration" in (ars[0].error or "")


@pytest.mark.asyncio
async def test_run_reiterate_with_plugin_rejects_parent_not_backtested(tmp_path, monkeypatch):
    from fwbg_agents.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path / "agents_data")

    db_url = f"sqlite+aiosqlite:///{tmp_path}/translator_reiter_plugin_pre.db"
    engine = create_async_engine(db_url, future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    parent_slug = "orb__forex__050"
    async with Session() as setup:
        now = datetime.now(UTC)
        s = Strategy(
            slug=parent_slug,
            current_state=StrategyState.PROPOSED.value,  # NOT backtested
            iteration_count=1,
            asset_class="FOREX",
            strategy_family="ORB",
            created_at=now,
            updated_at=now,
        )
        setup.add(s)
        await setup.commit()
        await setup.refresh(s)
        parent_id = s.id

    sidecar = _sidecar("indicators")
    async with Session() as session:
        parent = (
            await session.execute(select(Strategy).where(Strategy.id == parent_id))
        ).scalar_one()
        with pytest.raises(TranslatorError) as exc:
            await Translator(session).run_reiterate_with_plugin(parent, PLUGIN_SLUG, sidecar)
    assert "BACKTESTED" in str(exc.value)

    async with Session() as v:
        children = (
            (await v.execute(select(Strategy).where(Strategy.parent_strategy_id == parent_id)))
            .scalars()
            .all()
        )
        assert children == []
        ars = (await v.execute(select(AgentRun))).scalars().all()
        assert len(ars) == 1
        assert ars[0].status == AgentRunStatus.FAILED.value

    await engine.dispose()


@pytest.mark.asyncio
async def test_run_reiterate_with_plugin_appends_to_existing_iterations(db_with_parent):
    """When parent hypothesis already has iterations[], the new entry
    appends with iteration = existing + 1."""
    SessionMaker, parent_id, _parent_slug, it_dir = db_with_parent

    # Overwrite parent hypothesis to include a prior iterations block.
    parent_hypothesis_with_prior = {
        **PARENT_HYPOTHESIS,
        "iterations": [
            {
                "iteration": 1,
                "action": "add_indicator",
                "plugin_slug": "previous-plugin",
                "phase": "indicator",
                "capability": "previous capability",
                "rationale": "first iteration rationale",
            }
        ],
    }
    (it_dir / "hypothesis.json").write_text(json.dumps(parent_hypothesis_with_prior, indent=2))

    sidecar = _sidecar("indicators")

    async with SessionMaker() as session:
        parent = (
            await session.execute(select(Strategy).where(Strategy.id == parent_id))
        ).scalar_one()
        child = await Translator(session).run_reiterate_with_plugin(parent, PLUGIN_SLUG, sidecar)

    from fwbg_agents.config import settings

    hypothesis_path = (
        settings.data_dir / "strategies" / child.slug / "iteration_001" / "hypothesis.json"
    )
    data = json.loads(hypothesis_path.read_text())
    assert len(data["iterations"]) == 2
    assert data["iterations"][0]["plugin_slug"] == "previous-plugin"
    assert data["iterations"][1]["iteration"] == 2
    assert data["iterations"][1]["plugin_slug"] == PLUGIN_SLUG
