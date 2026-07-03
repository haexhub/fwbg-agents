"""fetch_live_catalog — live fwbg API catalog with offline fallback."""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from fwbg_agents.orchestrator.live_catalog import fetch_live_catalog, researcher_summary
from fwbg_agents.persistence.database import Base


class _FakeFwbg:
    async def get_plugins(self):
        return [
            {"name": "adx", "phase": "indicators", "description": "trend strength",
             "defaults": {"period": 14}},
            {"name": "xgboost", "phase": "model", "description": "", "defaults": {}},
            {"name": "atr_based", "phase": "exit_strategies", "description": "", "defaults": {}},
            # unmapped phase — must be ignored, not crashed on
            {"name": "labeler", "phase": "labeling", "description": "", "defaults": {}},
        ]

    async def get_exit_modifiers(self):
        return [{"name": "trailing_stop", "description": "ATR trail", "defaults": {}}]

    async def get_entry_modifiers(self):
        return []

    async def get_presets(self, section):
        return [{"id": f"{section}_preset_v1"}] if section == "validations" else []

    async def get_datasources(self):
        return [{"type": "csv", "name": "eur-usd", "path": "/data"}]

    async def get_datasource_assets(self):
        return {"assets": [
            {"symbol": "EURUSD", "timeframes": ["HOUR_1"], "source": "eur-usd"},
            {"symbol": "ORPHAN", "timeframes": ["DAY_1"], "source": "other"},
        ]}

    async def get_assets(self):
        return [
            {"symbol": "GBPUSD", "asset_class": "FOREX", "currencies": ["GBP"]},
            {"symbol": "EURUSD", "asset_class": "FOREX", "currencies": ["EUR"]},
            {"symbol": "DAX", "asset_class": "INDEX", "currencies": ["EUR"]},
        ]

    async def get_timeframes(self):
        return ["MINUTE_1", "MINUTE_15", "HOUR_1", "DAY_1"]

    async def get_dukascopy_instruments(self):
        return [
            {"symbol": "EURUSD", "group": "Forex",
             "historyStart": {"minute": "2003-05-04", "hourly": "2003-05-04",
                              "daily": "1973-03-01"}},
        ]


class _BrokenFwbg:
    def __getattr__(self, name):
        async def _fail(*a, **kw):
            raise ConnectionError("fwbg down")
        return _fail


@pytest_asyncio.fixture
async def session(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/lc.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as s:
        yield s
    await engine.dispose()


@pytest.mark.asyncio
async def test_fetch_builds_catalog_from_api(session):
    live = await fetch_live_catalog(session, _FakeFwbg())

    assert live.from_api
    assert live.catalog.all_slugs_for("indicators") == ["adx"]
    assert live.catalog.all_slugs_for("models") == ["xgboost"]
    assert live.catalog.all_slugs_for("exit_strategies") == ["atr_based"]
    # unmapped phases (labeling) are ignored, not crashed on
    assert "labeling" not in live.catalog.by_category
    assert live.plugin_details["indicators"][0]["default_params"] == {"period": 14}
    assert live.presets["validations"] == ["validations_preset_v1"]
    assert live.exit_modifiers[0]["name"] == "trailing_stop"
    # datasources carry their actual data availability
    assert live.datasource_names() == ["eur-usd"]
    assert live.datasources[0]["assets"] == [
        {"symbol": "EURUSD", "timeframes": ["HOUR_1"]}
    ]
    # the downloadable universe comes from the asset registry, sorted per
    # class, with history depth where the Dukascopy catalogue knows it
    assert live.asset_registry == {
        "FOREX": [
            {"symbol": "EURUSD",
             "history_start": {"minute": "2003-05-04", "hourly": "2003-05-04",
                               "daily": "1973-03-01"}},
            {"symbol": "GBPUSD"},
        ],
        "INDEX": [{"symbol": "DAX"}],
    }
    assert live.timeframes == ["MINUTE_1", "MINUTE_15", "HOUR_1", "DAY_1"]


@pytest.mark.asyncio
async def test_fetch_falls_back_to_filesystem_when_fwbg_down(session, tmp_path, monkeypatch):
    from fwbg_agents.config import settings
    from fwbg_agents.orchestrator import plugin_catalog

    # Point the filesystem scan at an empty repo so the test is hermetic.
    monkeypatch.setattr(settings, "fwbg_repo_root", tmp_path / "empty")
    plugin_catalog._load_fwbg_cached.cache_clear()

    live = await fetch_live_catalog(session, _BrokenFwbg())

    assert not live.from_api
    assert live.presets == {}
    assert live.datasources == []


@pytest.mark.asyncio
async def test_researcher_summary_is_slim(session):
    live = await fetch_live_catalog(session, _FakeFwbg())
    summary = researcher_summary(live)
    assert summary["indicators"] == [{"name": "adx", "description": "trend strength"}]
    assert "default_params" not in str(summary["indicators"])
    assert summary["datasources"][0]["name"] == "eur-usd"
    assert [e["symbol"] for e in summary["asset_registry"]["FOREX"]] == ["EURUSD", "GBPUSD"]
