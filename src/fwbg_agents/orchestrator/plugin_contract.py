"""PluginContract — pydantic schema for the contract.yaml that each agent-
authored plugin ships alongside its plugin.py.

The contract is the only LLM-bypass-resistant validator we have for plugin
behaviour. M5a defines + parses the schema; M5b's PluginEvaluator runs the
invariants against synthetic test_scenarios before letting the plugin
transition AUTHORED → VERIFIED.

`kind` mirrors the fwbg plugin category names (indicators, models, ...) so
catalog lookups and contract.kind line up 1:1. `PluginKindLit` is the
canonical type — `persistence.models.PluginKind` mirrors it via Migration
0004 (M5b) and the alignment is pinned by
`tests/persistence/test_verification_run.py::test_plugin_kind_enum_values_match_plugin_contract_literal`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

PluginKindLit = Literal[
    "indicator",
    "model",
    "exit_strategy",
    "risk_management",
    "entry_modifier",
    "preprocessing",
    "feature_selection",
    "data_loading",
]

InputDtype = Literal["float", "int", "bool", "series", "ohlcv"]
OutputDtype = Literal["series", "scalar", "boolean_series"]
ParamDtype = Literal["float", "int", "bool", "str"]
LengthInvariant = Literal["same_as_input", "trimmed", "any"]


class PluginContractError(ValueError):
    """Raised when contract.yaml fails to parse or validate."""


class PluginContractInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    dtype: InputDtype
    required: bool = True
    description: str = ""


class PluginContractOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    dtype: OutputDtype
    length_invariant: LengthInvariant = "same_as_input"


class PluginContractParam(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    dtype: ParamDtype
    default: Any
    min: float | None = None
    max: float | None = None
    description: str = ""


class PluginContractScenario(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    data_path: str
    expected_outputs: dict[str, Any] | None = None


class PluginContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    kind: PluginKindLit
    version: str = "v1"
    inputs: list[PluginContractInput]
    outputs: list[PluginContractOutput]
    params: list[PluginContractParam]
    invariants: list[str]
    test_scenarios: list[PluginContractScenario]

    @model_validator(mode="after")
    def _indicator_needs_invariants(self) -> PluginContract:
        if self.kind == "indicator" and not self.invariants:
            raise ValueError(
                "indicator contracts must declare at least one invariant — "
                "the Evaluator has nothing else to check against"
            )
        return self


def load_contract(path: Path) -> PluginContract:
    """Parse contract.yaml. Raises PluginContractError on any schema mismatch."""
    try:
        data = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise PluginContractError(f"cannot read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise PluginContractError(f"{path}: top-level YAML must be a mapping")
    try:
        return PluginContract.model_validate(data)
    except ValidationError as exc:
        raise PluginContractError(f"{path}: {exc}") from exc


def dump_contract(contract: PluginContract, path: Path) -> None:
    """Write contract.yaml with stable key order (pydantic insertion order)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = contract.model_dump(mode="json")
    path.write_text(yaml.safe_dump(data, sort_keys=False, default_flow_style=False))
