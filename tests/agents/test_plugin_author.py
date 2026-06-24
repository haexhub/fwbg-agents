"""PluginAuthor agent tests.

Same FunctionModel-stub pattern as test_analyst.py — no real LLM is called.
Tests cover:
- happy path (3 files written, plugin row in AUTHORED, transition + AgentRun done)
- slug collision (raises, no files written)
- failure marks AgentRun failed
- get_fwbg_plugin_examples unit test (clamping)
- validate_python_syntax unit test
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
import pytest_asyncio
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from fwbg_agents.agents.plugin_author import (
    PluginAuthor,
    PluginAuthorFailed,
    get_fwbg_plugin_examples,
    validate_python_syntax,
)
from fwbg_agents.orchestrator.plugin_catalog import (
    PluginCatalog,
    PluginManifest,
    _load_fwbg_cached,
)
from fwbg_agents.persistence.database import Base
from fwbg_agents.persistence.models import (
    AgentRun,
    AgentRunStatus,
    Plugin,
    PluginState,
    Strategy,
    StrategyState,
    Transition,
)


def _stub_model(args: dict) -> FunctionModel:
    """FunctionModel that emits one final_result tool call.

    `final_result` (no suffix) is the pydantic-ai tool name when output_type is
    a single BaseModel. The suffix `_<Variant>` only appears for discriminated
    unions (see test_analyst.py).
    """

    def handler(_messages, _info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[ToolCallPart("final_result", args)])

    return FunctionModel(handler)


_VALID_PLUGIN_CODE = (
    "import pandas as pd\n"
    "\n"
    "def compute(df: pd.DataFrame, *, window: int = 14) -> pd.Series:\n"
    "    return df['close'].rolling(window).mean()\n"
)
_VALID_CONTRACT = {
    "name": "fancy-ma",
    "kind": "indicator",
    "version": "v1",
    "inputs": [{"name": "ohlcv", "dtype": "ohlcv", "required": True, "description": ""}],
    "outputs": [{"name": "ma", "dtype": "series", "length_invariant": "same_as_input"}],
    "params": [{"name": "window", "dtype": "int", "default": 14, "min": 2, "max": 200, "description": ""}],
    "invariants": ["outputs[0].length == inputs[0].length"],
    "test_scenarios": [{"name": "trending_up", "data_path": "test_scenarios/trending_up.parquet"}],
}
_VALID_SPEC_MD = (
    "# fancy-ma\n\n"
    "A rolling mean of the close price over a configurable window. "
    "Useful as a baseline indicator for trend-following strategies.\n"
)


@pytest_asyncio.fixture
async def db_with_parent_strategy(tmp_path, monkeypatch):
    from fwbg_agents.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path / "agents_data")
    # Empty fwbg root so the catalog snapshot is clean and slug-collision logic
    # is driven purely by the DB.
    monkeypatch.setattr(settings, "fwbg_repo_root", tmp_path / "no-fwbg")
    _load_fwbg_cached.cache_clear()

    db_url = f"sqlite+aiosqlite:///{tmp_path}/author.db"
    engine = create_async_engine(db_url, future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async with Session() as setup:
        now = datetime.now(UTC)
        s = Strategy(
            slug="parent_v1",
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
        parent_id = s.id

    # Pre-seed iteration_001 with strategy.json + add_indicator_request.json
    it_dir = settings.data_dir / "strategies" / "parent_v1" / "iteration_001"
    it_dir.mkdir(parents=True, exist_ok=True)
    (it_dir / "strategy.json").write_text(
        json.dumps({"name": "parent_v1", "pipeline": "orb_pipeline"})
    )
    sidecar = it_dir / "add_indicator_request.json"
    sidecar.write_text(
        json.dumps(
            {
                "kind": "add_indicator",
                "confidence": 0.7,
                "reasoning": "no rolling-mean variant in catalog",
                "phase": "indicators",
                "capability": "rolling close-price mean",
                "category": "indicator",
                "strategy_id": parent_id,
                "strategy_slug": "parent_v1",
                "requested_at": now.isoformat(),
            }
        )
    )

    yield Session, parent_id, sidecar, tmp_path
    await engine.dispose()


async def test_author_writes_three_files_and_transitions_to_authored(
    db_with_parent_strategy,
):
    from fwbg_agents.config import settings

    SessionMaker, parent_id, sidecar, _tmp = db_with_parent_strategy
    model = _stub_model(
        {
            "slug": "fancy-ma",
            "python_code": _VALID_PLUGIN_CODE,
            "contract": _VALID_CONTRACT,
            "spec_md": _VALID_SPEC_MD,
        }
    )
    async with SessionMaker() as session:
        parent = (
            await session.execute(select(Strategy).where(Strategy.id == parent_id))
        ).scalar_one()
        author = PluginAuthor(session, model=model)
        plugin_id = await author.run_fresh(sidecar_path=sidecar, parent_strategy=parent)

    plugin_dir = settings.data_dir / "plugins" / "fancy-ma" / "v1"
    assert (plugin_dir / "plugin.py").exists()
    assert (plugin_dir / "contract.yaml").exists()
    assert (plugin_dir / "spec.md").exists()
    assert "rolling" in (plugin_dir / "plugin.py").read_text()

    async with SessionMaker() as v:
        plugin = (
            await v.execute(select(Plugin).where(Plugin.id == plugin_id))
        ).scalar_one()
        assert plugin.slug == "fancy-ma"
        assert plugin.kind == "indicator"
        assert plugin.current_state == PluginState.AUTHORED.value
        assert plugin.contract_path.endswith("contract.yaml")
        assert plugin.spec_path.endswith("spec.md")

        transitions = (
            await v.execute(
                select(Transition).where(Transition.entity_id == plugin_id)
            )
        ).scalars().all()
        # SPECIFIED → AUTHORED — one transition
        assert len(transitions) == 1
        assert transitions[0].from_state == PluginState.SPECIFIED.value
        assert transitions[0].to_state == PluginState.AUTHORED.value
        assert transitions[0].created_by == "plugin_author"
        assert transitions[0].payload["request_path"].endswith(
            "add_indicator_request.json"
        )
        assert transitions[0].payload["request_strategy_id"] == parent_id

        runs = (await v.execute(select(AgentRun))).scalars().all()
        assert len(runs) == 1
        assert runs[0].agent_name == "plugin_author"
        assert runs[0].status == AgentRunStatus.DONE.value
        assert runs[0].input_artifact_path.endswith("add_indicator_request.json")
        assert runs[0].output_artifact_path.endswith("contract.yaml")


async def test_author_slug_collision_raises_and_no_files_written(
    db_with_parent_strategy,
):
    from fwbg_agents.config import settings

    SessionMaker, parent_id, sidecar, _tmp = db_with_parent_strategy
    # Pre-seed a verified plugin with the same slug — collision via catalog.
    async with SessionMaker() as setup:
        now = datetime.now(UTC)
        existing = Plugin(
            slug="fancy-ma",
            current_state=PluginState.VERIFIED.value,
            kind="indicator",
            spec_path=str(settings.data_dir / "plugins" / "fancy-ma" / "v1" / "spec.md"),
            contract_path=str(
                settings.data_dir / "plugins" / "fancy-ma" / "v1" / "contract.yaml"
            ),
            created_at=now,
            updated_at=now,
        )
        setup.add(existing)
        await setup.commit()

    model = _stub_model(
        {
            "slug": "fancy-ma",
            "python_code": _VALID_PLUGIN_CODE,
            "contract": _VALID_CONTRACT,
            "spec_md": _VALID_SPEC_MD,
        }
    )

    async with SessionMaker() as session:
        parent = (
            await session.execute(select(Strategy).where(Strategy.id == parent_id))
        ).scalar_one()
        with pytest.raises(PluginAuthorFailed):
            await PluginAuthor(session, model=model).run_fresh(
                sidecar_path=sidecar, parent_strategy=parent
            )

    # Critically: no new files written to the would-be plugin dir
    plugin_dir = settings.data_dir / "plugins" / "fancy-ma" / "v1"
    assert not (plugin_dir / "plugin.py").exists()
    assert not (plugin_dir / "spec.md").exists()

    # AgentRun marked failed
    async with SessionMaker() as v:
        runs = (await v.execute(select(AgentRun))).scalars().all()
        assert len(runs) == 1
        assert runs[0].status == AgentRunStatus.FAILED.value
        assert "slug" in runs[0].error.lower()


async def test_author_missing_sidecar_raises(db_with_parent_strategy, tmp_path):
    SessionMaker, parent_id, _existing_sidecar, _tmp = db_with_parent_strategy
    missing = tmp_path / "does-not-exist.json"
    model = _stub_model(
        {
            "slug": "fancy-ma",
            "python_code": _VALID_PLUGIN_CODE,
            "contract": _VALID_CONTRACT,
            "spec_md": _VALID_SPEC_MD,
        }
    )
    async with SessionMaker() as session:
        parent = (
            await session.execute(select(Strategy).where(Strategy.id == parent_id))
        ).scalar_one()
        with pytest.raises(FileNotFoundError):
            await PluginAuthor(session, model=model).run_fresh(
                sidecar_path=missing, parent_strategy=parent
            )


def test_validate_python_syntax_ok():
    res = validate_python_syntax("def f():\n    return 1\n")
    assert res.ok is True
    assert res.line is None
    assert res.msg == ""


def test_validate_python_syntax_error_reports_line():
    res = validate_python_syntax("def f(:\n    pass\n")
    assert res.ok is False
    assert res.line == 1
    assert "syntax" in res.msg.lower() or "invalid" in res.msg.lower()


def test_get_fwbg_plugin_examples_clamps_above_5(tmp_path, caplog):
    """When n > 5, the function silently clamps at 5 and logs a warning."""
    # Build a fake bundle with 7 indicator slugs
    bundle = tmp_path / "fake-fwbg" / "src" / "fwbg" / "plugins" / "fwbg-core"
    bundle.mkdir(parents=True)
    manifest = {
        "name": "fwbg-core",
        "version": "1.0.0",
        "plugins": {"indicators": [f"ind{i}" for i in range(7)]},
    }
    (bundle / "manifest.json").write_text(json.dumps(manifest))
    for i in range(7):
        d = bundle / "indicators" / f"ind{i}"
        d.mkdir(parents=True)
        (d / "__init__.py").write_text("")
        (d / "plugin.py").write_text(f"def compute(df, n={i}):\n    return df\n")

    catalog = PluginCatalog(
        by_category={
            "indicators": {
                f"ind{i}": PluginManifest(
                    name=f"ind{i}",
                    category="indicators",
                    provenance="fwbg-core",
                    version="1.0.0",
                    source_path=bundle / "manifest.json",
                )
                for i in range(7)
            }
        }
    )

    with caplog.at_level("WARNING"):
        examples = get_fwbg_plugin_examples(catalog, category="indicator", n=99)
    assert len(examples) == 5
    assert any("clamp" in r.getMessage().lower() for r in caplog.records)


def test_get_fwbg_plugin_examples_returns_truncated_source(tmp_path):
    bundle = tmp_path / "fake-fwbg" / "src" / "fwbg" / "plugins" / "fwbg-core"
    bundle.mkdir(parents=True)
    (bundle / "manifest.json").write_text(
        json.dumps({"name": "fwbg-core", "version": "1.0.0", "plugins": {"indicators": ["ema"]}})
    )
    d = bundle / "indicators" / "ema"
    d.mkdir(parents=True)
    # 6000-char source — should be truncated to ≤ 4000
    (d / "plugin.py").write_text("x = 1\n" + ("# pad\n" * 1500))
    catalog = PluginCatalog(
        by_category={
            "indicators": {
                "ema": PluginManifest(
                    name="ema",
                    category="indicators",
                    provenance="fwbg-core",
                    version="1.0.0",
                    source_path=bundle / "manifest.json",
                )
            }
        }
    )
    examples = get_fwbg_plugin_examples(catalog, category="indicator", n=1)
    assert len(examples) == 1
    assert examples[0].slug == "ema"
    assert len(examples[0].source) <= 4000


def test_get_fwbg_plugin_examples_unknown_category_returns_empty():
    examples = get_fwbg_plugin_examples(
        PluginCatalog(by_category={}), category="indicator", n=3
    )
    assert examples == []
