"""PluginEvaluator tests — deterministic, no LLM.

End-to-end across the verification lifecycle:
- happy path: plugin passes all scenarios → VerificationRun.status=passed,
  Plugin transitions AUTHORED → VERIFIED, parquets written, no error log.
- length mismatch → status=failed, plugin stays AUTHORED, structured JSON
  error log appears.
- unknown scenario in contract → whole evaluation fails immediately.
- empty test_scenarios in contract → whole evaluation fails.
- plugin.py without compute() → whole evaluation fails.
- second failed run overwrites the first error log.
- evaluator records started_at / ended_at timestamps.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from fwbg_agents.agents.plugin_evaluator import PluginEvaluator
from fwbg_agents.orchestrator.plugin_contract import (
    PluginContract,
    PluginContractInput,
    PluginContractOutput,
    PluginContractParam,
    PluginContractScenario,
    dump_contract,
)
from fwbg_agents.persistence.database import Base
from fwbg_agents.persistence.models import (
    Plugin,
    PluginState,
    VerificationRun,
)


def _good_contract(slug: str, scenarios: list[str]) -> PluginContract:
    return PluginContract(
        name=slug,
        kind="indicator",
        version="v1",
        inputs=[PluginContractInput(name="ohlcv", dtype="ohlcv")],
        outputs=[PluginContractOutput(name="ma", dtype="series", length_invariant="same_as_input")],
        params=[
            PluginContractParam(name="window", dtype="int", default=14, min=2, max=200)
        ],
        invariants=["outputs[0].length == inputs[0].length"],
        test_scenarios=[
            PluginContractScenario(name=s, data_path=f"test_scenarios/{s}.parquet")
            for s in scenarios
        ],
    )


_GOOD_PLUGIN_CODE = """
import pandas as pd

def compute(df: pd.DataFrame, *, window: int = 14) -> pd.Series:
    return df['close'].rolling(window, min_periods=1).mean()
"""

_LENGTH_MISMATCH_CODE = """
import pandas as pd

def compute(df: pd.DataFrame, *, window: int = 14) -> pd.Series:
    return df['close'].head(5)  # wrong length
"""

_NO_COMPUTE_CODE = """
def some_other_callable(df):
    return df
"""


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    from fwbg_agents.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path / "agents_data")
    db_url = f"sqlite+aiosqlite:///{tmp_path}/eval.db"
    engine = create_async_engine(db_url, future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as session:
        yield session, settings
    await engine.dispose()


async def _seed_plugin(
    session,
    settings,
    *,
    slug: str,
    code: str,
    scenarios: list[str],
) -> Plugin:
    """Write plugin.py + contract.yaml + create an AUTHORED Plugin row."""
    target = settings.data_dir / "plugins" / slug / "v1"
    target.mkdir(parents=True, exist_ok=True)
    (target / "plugin.py").write_text(code)
    dump_contract(_good_contract(slug, scenarios), target / "contract.yaml")

    now = datetime.now(UTC)
    p = Plugin(
        slug=slug,
        current_state=PluginState.SPECIFIED.value,
        kind="indicator",
        spec_path=str(target / "spec.md"),
        contract_path=str(target / "contract.yaml"),
        created_at=now,
        updated_at=now,
    )
    session.add(p)
    await session.flush()
    # Step it forward to AUTHORED via a manual Transition row + state update
    # (we don't need transition_plugin for the test setup).
    from fwbg_agents.orchestrator.lifecycle import transition_plugin

    await transition_plugin(
        session,
        p,
        PluginState.AUTHORED,
        reason="test setup",
        payload={},
        created_by="test",
    )
    await session.commit()
    await session.refresh(p)
    return p


async def test_evaluator_happy_path_passes_and_transitions_to_verified(db):
    session, settings = db
    p = await _seed_plugin(
        session, settings, slug="ma14", code=_GOOD_PLUGIN_CODE,
        scenarios=["trending_up", "sideways"],
    )

    vr_id = await PluginEvaluator(session).run(p)

    vr = (
        await session.execute(select(VerificationRun).where(VerificationRun.id == vr_id))
    ).scalar_one()
    assert vr.status == "passed"
    assert vr.scenarios_run == 2
    assert vr.scenarios_passed == 2
    assert vr.error_log_path is None
    assert vr.ended_at is not None
    assert vr.ended_at >= vr.started_at

    await session.refresh(p)
    assert p.current_state == PluginState.VERIFIED.value

    parquet_dir = settings.data_dir / "plugins" / "ma14" / "v1" / "test_scenarios"
    assert (parquet_dir / "trending_up.parquet").exists()
    assert (parquet_dir / "sideways.parquet").exists()
    error_log = settings.data_dir / "plugins" / "ma14" / "v1" / "error_log.json"
    assert not error_log.exists()


async def test_evaluator_length_mismatch_stays_authored(db):
    session, settings = db
    p = await _seed_plugin(
        session, settings, slug="bad-len", code=_LENGTH_MISMATCH_CODE,
        scenarios=["trending_up"],
    )

    vr_id = await PluginEvaluator(session).run(p)

    vr = (
        await session.execute(select(VerificationRun).where(VerificationRun.id == vr_id))
    ).scalar_one()
    assert vr.status == "failed"
    assert vr.scenarios_run == 1
    assert vr.scenarios_passed == 0

    await session.refresh(p)
    assert p.current_state == PluginState.AUTHORED.value

    error_log = Path(vr.error_log_path)
    assert error_log.exists()
    payload = json.loads(error_log.read_text())
    assert payload["verification_run_id"] == vr_id
    assert len(payload["errors"]) >= 1
    assert any("length" in e["invariant_violated"].lower() for e in payload["errors"])


async def test_evaluator_unknown_scenario_in_contract_fails(db):
    session, settings = db
    p = await _seed_plugin(
        session, settings, slug="bad-scn", code=_GOOD_PLUGIN_CODE,
        scenarios=["does_not_exist"],
    )

    vr_id = await PluginEvaluator(session).run(p)
    vr = (
        await session.execute(select(VerificationRun).where(VerificationRun.id == vr_id))
    ).scalar_one()
    assert vr.status == "failed"
    payload = json.loads(Path(vr.error_log_path).read_text())
    assert payload["errors"][0]["invariant_violated"] == "unknown_scenario"

    await session.refresh(p)
    assert p.current_state == PluginState.AUTHORED.value


async def test_evaluator_empty_scenarios_fails(db):
    session, settings = db
    p = await _seed_plugin(
        session, settings, slug="empty-scn", code=_GOOD_PLUGIN_CODE, scenarios=[],
    )

    vr_id = await PluginEvaluator(session).run(p)
    vr = (
        await session.execute(select(VerificationRun).where(VerificationRun.id == vr_id))
    ).scalar_one()
    assert vr.status == "failed"
    payload = json.loads(Path(vr.error_log_path).read_text())
    assert payload["errors"][0]["invariant_violated"] == "no_scenarios_declared"
    await session.refresh(p)
    assert p.current_state == PluginState.AUTHORED.value


async def test_evaluator_no_compute_callable_fails(db):
    session, settings = db
    p = await _seed_plugin(
        session, settings, slug="no-compute", code=_NO_COMPUTE_CODE,
        scenarios=["trending_up"],
    )

    vr_id = await PluginEvaluator(session).run(p)
    vr = (
        await session.execute(select(VerificationRun).where(VerificationRun.id == vr_id))
    ).scalar_one()
    assert vr.status == "failed"
    payload = json.loads(Path(vr.error_log_path).read_text())
    assert any("compute" in e["invariant_violated"].lower() for e in payload["errors"])


async def test_evaluator_second_run_overwrites_error_log(db):
    session, settings = db
    p = await _seed_plugin(
        session, settings, slug="rerun", code=_LENGTH_MISMATCH_CODE,
        scenarios=["trending_up"],
    )

    vr1_id = await PluginEvaluator(session).run(p)
    vr2_id = await PluginEvaluator(session).run(p)
    assert vr1_id != vr2_id

    error_log = settings.data_dir / "plugins" / "rerun" / "v1" / "error_log.json"
    payload = json.loads(error_log.read_text())
    # Only the latest run's id should be on disk
    assert payload["verification_run_id"] == vr2_id

    rows = (
        await session.execute(select(VerificationRun).where(VerificationRun.plugin_id == p.id))
    ).scalars().all()
    assert len(rows) == 2
