"""PluginEvaluator — deterministic verification of an agent-authored plugin.

Locked decisions from M5a:
- Deterministic only. No LLM in M5b — the evaluator is pure Python.
- Hand-curated, np-seeded scenarios under
  `data/plugins/<slug>/v1/test_scenarios/` — see scenario_generators.
- Failed verification keeps the plugin in AUTHORED. Manual retry only;
  no auto-abandon counter in M5b.
- Structured JSON error log so the dashboard can parse it later.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import traceback
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd  # type: ignore[import-untyped]
from sqlalchemy.ext.asyncio import AsyncSession

from fwbg_agents.orchestrator.lifecycle import plugin_dir, transition_plugin
from fwbg_agents.orchestrator.plugin_contract import (
    PluginContract,
    PluginContractScenario,
    load_contract,
)
from fwbg_agents.orchestrator.scenario_generators import (
    SCENARIO_GENERATORS,
    generate_scenario,
)
from fwbg_agents.persistence.models import (
    Plugin,
    PluginState,
    VerificationRun,
)
from fwbg_agents.run_events import emit_run_event

log = logging.getLogger(__name__)


class PluginEvaluator:
    """Deterministic verifier that runs a plugin's contract scenarios and transitions state."""

    def __init__(self, session: AsyncSession):
        """Initialize."""
        self.session = session

    async def run(self, plugin: Plugin, *, agent_run_id: int | None = None) -> int:
        """Verify a plugin against its contract. Returns verification_run.id."""
        now = datetime.now(UTC)
        vr = VerificationRun(
            plugin_id=plugin.id,
            status="running",
            scenarios_run=0,
            scenarios_passed=0,
            started_at=now,
            created_at=now,
        )
        self.session.add(vr)
        await self.session.flush()  # need vr.id for the error_log payload

        target_dir = plugin_dir(plugin.slug) / "v1"
        scenarios_dir = target_dir / "test_scenarios"
        scenarios_dir.mkdir(parents=True, exist_ok=True)
        error_log_path = target_dir / "error_log.json"

        errors: list[dict[str, Any]] = []

        try:
            contract = load_contract(Path(plugin.contract_path))  # type: ignore[arg-type]  # None caught by surrounding try as contract_load_failed
        except Exception as exc:
            errors.append(
                {
                    "scenario_name": "",
                    "invariant_violated": "contract_load_failed",
                    "traceback": "".join(traceback.format_exception(exc)),
                    "ts": datetime.now(UTC).isoformat(),
                }
            )
            return await self._finalise_failed(vr, errors, error_log_path)

        if not contract.test_scenarios:
            errors.append(
                {
                    "scenario_name": "",
                    "invariant_violated": "no_scenarios_declared",
                    "traceback": None,
                    "ts": datetime.now(UTC).isoformat(),
                }
            )
            return await self._finalise_failed(vr, errors, error_log_path)

        # Pre-flight: refuse the whole run if any declared scenario name has
        # no generator. A contract bug is not a plugin bug — staying strict
        # avoids partial-success ambiguity.
        for scenario in contract.test_scenarios:
            if scenario.name not in SCENARIO_GENERATORS:
                errors.append(
                    {
                        "scenario_name": scenario.name,
                        "invariant_violated": "unknown_scenario",
                        "traceback": None,
                        "ts": datetime.now(UTC).isoformat(),
                    }
                )
                return await self._finalise_failed(vr, errors, error_log_path)

        # Load compute() callable from the on-disk plugin.py
        try:
            compute = _load_compute(target_dir / "plugin.py")
        except _PluginLoadError as exc:
            errors.append(
                {
                    "scenario_name": "",
                    "invariant_violated": exc.reason,
                    "traceback": exc.tb,
                    "ts": datetime.now(UTC).isoformat(),
                }
            )
            return await self._finalise_failed(vr, errors, error_log_path)

        # Build the param-defaults dict once.
        param_defaults = {p.name: p.default for p in contract.params}

        # Run each scenario.
        for scenario in contract.test_scenarios:
            df = generate_scenario(scenario.name)
            (scenarios_dir / f"{scenario.name}.parquet").write_bytes(b"")  # placeholder
            df.to_parquet(scenarios_dir / f"{scenario.name}.parquet")

            vr.scenarios_run += 1
            scenario_errors = _evaluate_scenario(
                compute, df, contract=contract, scenario=scenario, params=param_defaults
            )
            if scenario_errors:
                errors.extend(scenario_errors)
                if agent_run_id is not None:
                    emit_run_event(
                        agent_run_id, "scenario_failed",
                        name=scenario.name,
                        index=vr.scenarios_run,
                        total=len(contract.test_scenarios),
                        invariant_violated=scenario_errors[0].get("invariant_violated"),
                    )
            else:
                vr.scenarios_passed += 1
                if agent_run_id is not None:
                    emit_run_event(
                        agent_run_id, "scenario_passed",
                        name=scenario.name,
                        index=vr.scenarios_run,
                        total=len(contract.test_scenarios),
                    )

        if agent_run_id is not None:
            emit_run_event(
                agent_run_id, "evaluation_done",
                scenarios_run=vr.scenarios_run,
                scenarios_passed=vr.scenarios_passed,
                status=(
                    "passed"
                    if vr.scenarios_passed == vr.scenarios_run and vr.scenarios_run > 0
                    else "failed"
                ),
            )

        if vr.scenarios_passed == vr.scenarios_run and vr.scenarios_run > 0:
            vr.status = "passed"
            vr.ended_at = datetime.now(UTC)
            await self.session.flush()

            await transition_plugin(
                self.session,
                plugin,
                PluginState.VERIFIED,
                reason="plugin_evaluator",
                payload={
                    "verification_run_id": vr.id,
                    "scenarios_passed": vr.scenarios_passed,
                },
                created_by="plugin_evaluator",
            )
            await self.session.commit()
            await self.session.refresh(vr)
            # Clear any stale error log from a previous failed run.
            if error_log_path.exists():
                error_log_path.unlink()
            return vr.id

        # else — failed.
        return await self._finalise_failed(vr, errors, error_log_path)

    async def _finalise_failed(
        self,
        vr: VerificationRun,
        errors: list[dict[str, Any]],
        error_log_path: Path,
    ) -> int:
        """Mark the verification run as failed, write the error log, and return its id."""
        vr.status = "failed"
        vr.ended_at = datetime.now(UTC)
        error_log_path.parent.mkdir(parents=True, exist_ok=True)
        error_log_path.write_text(
            json.dumps(
                {"verification_run_id": vr.id, "errors": errors},
                indent=2,
                default=str,
            )
        )
        vr.error_log_path = str(error_log_path)
        await self.session.commit()
        await self.session.refresh(vr)
        return vr.id


# ---------------------------------------------------------------------------
# Plugin loading + invariant checks (module-private helpers)
# ---------------------------------------------------------------------------


class _PluginLoadError(Exception):
    """Raised when a plugin module cannot be loaded or lacks a callable compute."""

    def __init__(self, reason: str, tb: str | None = None):
        """Initialize."""
        super().__init__(reason)
        self.reason = reason
        self.tb = tb


def _load_compute(plugin_py: Path):
    """Load and return the compute() callable from the given plugin.py path."""
    if not plugin_py.is_file():
        raise _PluginLoadError("plugin_py_missing")
    try:
        spec = importlib.util.spec_from_file_location(
            f"_plugin_under_test_{plugin_py.parent.parent.name}", plugin_py
        )
        if spec is None or spec.loader is None:
            raise _PluginLoadError("plugin_spec_failed")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except Exception as exc:
        raise _PluginLoadError(
            "plugin_import_failed", tb="".join(traceback.format_exception(exc))
        ) from exc
    compute = getattr(mod, "compute", None)
    if not callable(compute):
        raise _PluginLoadError("compute_callable_missing")
    return compute


def _evaluate_scenario(
    compute,
    df: pd.DataFrame,
    *,
    contract: PluginContract,
    scenario: PluginContractScenario,
    params: dict[str, Any],
) -> list[dict[str, Any]]:
    """Run compute() and check the contract's three hard-coded invariants:

    1. length parity — outputs marked ``same_as_input`` match ``len(df)``;
    2. finite / non-all-NaN — a numeric output must not be entirely NaN and
       must not contain ±inf (leading warm-up NaNs are legitimate and allowed);
    3. dtype sanity — an output's values must be numeric or boolean (an
       object/string-dtype indicator output is always a bug).

    Returns a list of error dicts; empty list means the scenario passed.
    """
    ts = lambda: datetime.now(UTC).isoformat()  # noqa: E731

    try:
        result = compute(df, **params)
    except Exception as exc:
        return [
            {
                "scenario_name": scenario.name,
                "invariant_violated": "compute_raised",
                "traceback": "".join(traceback.format_exception(exc)),
                "ts": ts(),
            }
        ]

    errors: list[dict[str, Any]] = []

    # Invariant 1: length parity for outputs marked same_as_input.
    output_lengths = _output_lengths(result, contract)
    expected_len = len(df)
    for declared in contract.outputs:
        length = output_lengths[declared.name]
        if declared.length_invariant == "same_as_input" and length != expected_len:
            errors.append(
                {
                    "scenario_name": scenario.name,
                    "invariant_violated": "length_mismatch",
                    "traceback": (
                        f"output {declared.name!r}: got len={length}, "
                        f"expected len={expected_len}"
                    ),
                    "ts": ts(),
                }
            )

    # Invariants 2 (finite / non-all-NaN) and 3 (dtype) — same value extraction
    # as the length check above.
    output_values = _output_values(result, contract)
    for declared in contract.outputs:
        value = output_values[declared.name]
        if value is None:
            continue  # a missing output is already flagged by length_mismatch
        errors.extend(
            _value_invariant_errors(value, declared.name, scenario.name, ts)
        )

    return errors


def _output_values(result: Any, contract: PluginContract) -> dict[str, Any]:
    """Map each declared output name to its observed value, mirroring the
    extraction convention of `_output_lengths` (single output → the result
    itself; multi output → a dict keyed by output.name)."""
    declared = contract.outputs
    if len(declared) == 1:
        return {declared[0].name: result}
    if isinstance(result, dict):
        return {o.name: result.get(o.name) for o in declared}
    return {o.name: None for o in declared}


def _value_invariant_errors(
    value: Any, output_name: str, scenario_name: str, ts
) -> list[dict[str, Any]]:
    """Finite (Invariant 2) and dtype (Invariant 3) checks for one output.

    Array-like values are inspected as a pandas Series; scalars are checked
    directly. An all-NaN or ±inf-containing numeric output, or a value that is
    neither numeric nor boolean, is a violation. Leading warm-up NaNs pass."""

    def _err(kind: str, detail: str) -> dict[str, Any]:
        return {
            "scenario_name": scenario_name,
            "invariant_violated": kind,
            "traceback": f"output {output_name!r}: {detail}",
            "ts": ts(),
        }

    if isinstance(value, pd.Series):
        series: pd.Series | None = value
    elif isinstance(value, np.ndarray):
        series = pd.Series(value)
    else:
        series = None

    if series is not None:
        is_numeric = pd.api.types.is_numeric_dtype(series)
        is_bool = pd.api.types.is_bool_dtype(series)
        if not (is_numeric or is_bool):
            return [_err("wrong_dtype", f"expected numeric/boolean, got dtype {series.dtype}")]
        if is_numeric and not is_bool:
            arr = series.to_numpy(dtype="float64", na_value=np.nan)
            if series.isna().all():
                return [_err("non_finite_output", "all values are NaN")]
            if np.isinf(arr).any():
                return [_err("non_finite_output", "contains ±inf")]
        return []

    # Scalar output (contract dtype="scalar").
    if isinstance(value, (bool, np.bool_)):
        return []
    if isinstance(value, (int, float, np.number)):
        if isinstance(value, (float, np.floating)) and not np.isfinite(value):
            return [_err("non_finite_output", "scalar value is NaN/inf")]
        return []
    return [_err("wrong_dtype", f"expected numeric/boolean scalar, got {type(value).__name__}")]


def _output_lengths(result: Any, contract: PluginContract) -> dict[str, int]:
    """Map each declared output name to the length we observed."""
    out: dict[str, int] = {}
    declared = contract.outputs

    # Single-output convention: result is a Series / 1-D array / scalar.
    if len(declared) == 1:
        out[declared[0].name] = _length_of(result)
        return out

    # Multi-output convention: result must be a dict keyed by output.name.
    if isinstance(result, dict):
        for o in declared:
            out[o.name] = _length_of(result.get(o.name))
    else:
        for o in declared:
            out[o.name] = -1  # signals mismatch
    return out


def _length_of(value: Any) -> int:
    """Return the length of value, or 1 for scalars, or -1 for None."""
    if value is None:
        return -1
    try:
        return len(value)
    except TypeError:
        # Scalar (0-D): length-0 reported as 1 for our purposes
        return 1
