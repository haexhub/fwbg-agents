"""Tests for orchestrator.plugin_contract — pydantic schema for contract.yaml.

The contract is the SINGLE source of truth that the M5b PluginEvaluator
checks against. M5a only handles parse/dump/validation; runtime invariant
enforcement lives in M5b.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from fwbg_agents.orchestrator.plugin_contract import (
    PluginContractError,
    dump_contract,
    load_contract,
)

EXAMPLE_PATH = (
    Path(__file__).resolve().parents[2].parent
    / "fwbg"
    / "docs"
    / "specs"
    / "plugin_contract.example.yaml"
)


def test_load_example_roundtrip():
    contract = load_contract(EXAMPLE_PATH)
    assert contract.name == "zone_pivots"
    assert contract.kind == "indicator"
    assert contract.version == "v1"
    assert len(contract.inputs) == 2
    assert contract.inputs[0].name == "ohlcv"
    assert contract.inputs[0].dtype == "ohlcv"
    assert len(contract.outputs) == 2
    assert contract.outputs[0].length_invariant == "same_as_input"
    assert len(contract.params) == 2
    assert contract.params[0].default == 20
    assert "outputs[0] same length as inputs[0]" in contract.invariants
    assert len(contract.test_scenarios) == 2


def test_dump_then_load_equal(tmp_path):
    original = load_contract(EXAMPLE_PATH)
    out = tmp_path / "contract.yaml"
    dump_contract(original, out)
    reloaded = load_contract(out)
    assert reloaded.model_dump() == original.model_dump()


def test_missing_kind_raises(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        yaml.safe_dump(
            {
                "name": "x",
                "inputs": [],
                "outputs": [],
                "params": [],
                "invariants": ["dummy"],
                "test_scenarios": [],
            }
        )
    )
    with pytest.raises(PluginContractError) as exc:
        load_contract(bad)
    assert "kind" in str(exc.value).lower()


def test_bogus_kind_raises(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        yaml.safe_dump(
            {
                "name": "x",
                "kind": "bogus",
                "inputs": [],
                "outputs": [],
                "params": [],
                "invariants": ["dummy"],
                "test_scenarios": [],
            }
        )
    )
    with pytest.raises(PluginContractError):
        load_contract(bad)


def test_empty_scenarios_allowed(tmp_path):
    """Some indicators legitimately have no test scenarios — parse must succeed."""
    f = tmp_path / "c.yaml"
    f.write_text(
        yaml.safe_dump(
            {
                "name": "trivial",
                "kind": "model",
                "inputs": [{"name": "x", "dtype": "series"}],
                "outputs": [{"name": "y", "dtype": "scalar"}],
                "params": [],
                "invariants": [],
                "test_scenarios": [],
            }
        )
    )
    contract = load_contract(f)
    assert contract.test_scenarios == []


def test_indicator_must_have_invariants(tmp_path):
    """An indicator with zero invariants is meaningless — Evaluator has nothing to check."""
    f = tmp_path / "c.yaml"
    f.write_text(
        yaml.safe_dump(
            {
                "name": "i",
                "kind": "indicator",
                "inputs": [{"name": "ohlcv", "dtype": "ohlcv"}],
                "outputs": [{"name": "y", "dtype": "series"}],
                "params": [],
                "invariants": [],
                "test_scenarios": [],
            }
        )
    )
    with pytest.raises(PluginContractError) as exc:
        load_contract(f)
    assert "invariant" in str(exc.value).lower()
