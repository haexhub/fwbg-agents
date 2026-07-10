"""M5d PluginImplementer tests — FunctionModel-stubbed, no real LLM.

Covers:
- happy path round 1
- recover-from-syntax-error round 1 → success round 2
- recover-from-contract-error round 1 → success round 2
- exhaust max_rounds → PluginImplementerError with last_code + last_err
- last_err appears in round-N prompt
- last_code appears in round-N prompt
- max_rounds respects settings env override
- contract_check static AST gate (unit tests)
- slug mismatch between plan and output is detected as a gate fail
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from fwbg_agents.agents.plugin_implementer import (
    PluginImplementer,
    PluginImplementerError,
    _render_implementer_prompt,
    contract_check,
    implementer_model,
)
from fwbg_agents.agents.plugin_planner import PluginPlan

# ---------------------------------------------------------------------------
# Plan / result fixtures
# ---------------------------------------------------------------------------


def _make_plan(
    slug: str = "fancy_indicator",
    class_name: str = "FancyIndicator",
    phase: str = "indicators",
) -> PluginPlan:
    return PluginPlan.model_validate(
        {
            "slug": slug,
            "class_name": class_name,
            "phase": phase,
            "version": "0.1.0",
            "stateful": False,
            "depends_on": [],
            "params": [
                {
                    "name": "window",
                    "type": "int",
                    "default": 14,
                    "description": "Rolling window in bars",
                    "min": 2,
                    "max": 200,
                    "step": 1,
                    "required": True,
                }
            ],
            "feature_columns": ["fancy_value"],
            "algorithm_sketch": (
                "Compute a rolling mean of close prices over a configurable window. "
                "Shift the resulting series by 1 bar to prevent lookahead bias. "
                "Append the column to the input DataFrame."
            ),
            "edge_cases": ["empty DataFrame", "window larger than series length"],
            "expected_test_names": [
                "test_constant_price_yields_constant_mean",
                "test_no_lookahead_bias",
                "test_default_params",
            ],
        }
    )


_VALID_CODE = (
    "from fwbg_sdk.indicators import BaseIndicator, shift_features\n"
    "from fwbg_sdk.base import PluginPhase\n"
    "import pandas as pd\n"
    "\n"
    "class FancyIndicator(BaseIndicator):\n"
    "    name = 'fancy_indicator'\n"
    "    phase = PluginPhase.INDICATORS\n"
    "    version = '0.1.0'\n"
    "\n"
    "    def compute(self, df: pd.DataFrame, *, window: int = 14) -> pd.DataFrame:\n"
    "        features = {'fancy_value': df['close'].rolling(window).mean()}\n"
    "        return pd.concat([df, shift_features(features, df.index)], axis=1)\n"
    "\n"
    "    def get_feature_columns(self):\n"
    "        return ['fancy_value']\n"
)
_SYNTAX_BROKEN_CODE = (
    "from fwbg_sdk.indicators import BaseIndicator\n"
    "\n"
    "class FancyIndicator(BaseIndicator):\n"
    "    name = 'fancy_indicator'\n"
    "    def compute(self, df:\n"  # truncated — SyntaxError
)
_WRONG_CLASS_CODE = (
    "from fwbg_sdk.indicators import BaseIndicator\n"
    "class TotallyDifferentName(BaseIndicator):\n"
    "    name = 'fancy_indicator'\n"
    "    def compute(self, df, **p): return df\n"
)
_VALID_CONTRACT = {
    "name": "fancy_indicator",
    "kind": "indicator",
    "version": "v1",
    "inputs": [
        {"name": "ohlcv", "dtype": "ohlcv", "required": True, "description": ""}
    ],
    "outputs": [
        {"name": "fancy_value", "dtype": "series", "length_invariant": "same_as_input"}
    ],
    "params": [
        {
            "name": "window",
            "dtype": "int",
            "default": 14,
            "min": 2,
            "max": 200,
            "description": "",
        }
    ],
    "invariants": ["outputs[0].length == inputs[0].length"],
    "test_scenarios": [
        {"name": "trending_up", "data_path": "test_scenarios/trending_up.parquet"}
    ],
}
_VALID_SPEC = (
    "# fancy_indicator\n\n"
    "A rolling mean of the close price over a configurable window. Useful as "
    "a baseline trend-following feature. Shift by 1 bar to avoid lookahead.\n"
)


def _result_args(code: str = _VALID_CODE, slug: str = "fancy_indicator") -> dict[str, Any]:
    return {
        "slug": slug,
        "python_code": code,
        "contract": _VALID_CONTRACT,
        "spec_md": _VALID_SPEC,
    }


def _stateful_model(*responses: dict[str, Any]) -> FunctionModel:
    """FunctionModel that returns the i-th response on the i-th agent.run() call."""
    counter = {"i": 0}

    def handler(_messages, _info: AgentInfo) -> ModelResponse:
        idx = counter["i"]
        counter["i"] += 1
        if idx >= len(responses):
            raise IndexError(f"_stateful_model: ran out of stubbed responses at call {idx}")
        return ModelResponse(parts=[ToolCallPart("final_result", responses[idx])])

    return FunctionModel(handler)


def _capturing_model(*responses: dict[str, Any], capture: list[Any]) -> FunctionModel:
    counter = {"i": 0}

    def handler(messages, info: AgentInfo) -> ModelResponse:
        capture.append({"messages": messages})
        idx = counter["i"]
        counter["i"] += 1
        return ModelResponse(parts=[ToolCallPart("final_result", responses[idx])])

    return FunctionModel(handler)


# ---------------------------------------------------------------------------
# contract_check unit tests
# ---------------------------------------------------------------------------


def test_contract_check_accepts_valid_indicator():
    plan = _make_plan()
    check = contract_check(_VALID_CODE, plan)
    assert check.ok, check.msg


def test_contract_check_rejects_missing_class():
    plan = _make_plan(class_name="MissingClass")
    check = contract_check(_VALID_CODE, plan)
    assert not check.ok
    assert "not found" in check.msg


def test_contract_check_rejects_wrong_base_class():
    plan = _make_plan()
    code = _VALID_CODE.replace("BaseIndicator", "BasePreprocessor")
    check = contract_check(code, plan)
    assert not check.ok
    assert "must inherit from BaseIndicator" in check.msg


def test_contract_check_rejects_wrong_name_attr():
    plan = _make_plan()
    code = _VALID_CODE.replace("name = 'fancy_indicator'", "name = 'wrong_slug'")
    check = contract_check(code, plan)
    assert not check.ok
    assert "must equal slug 'fancy_indicator'" in check.msg


def test_contract_check_rejects_wrong_phase_attr():
    plan = _make_plan()
    code = _VALID_CODE.replace(
        "phase = PluginPhase.INDICATORS", "phase = PluginPhase.PREPROCESSING"
    )
    check = contract_check(code, plan)
    assert not check.ok
    assert "PluginPhase.INDICATORS" in check.msg


def test_contract_check_accepts_inherited_phase_attr():
    """phase attr is optional on the subclass when inherited from the base."""
    plan = _make_plan()
    # Strip the explicit `phase = PluginPhase.INDICATORS` line.
    code = "\n".join(
        line for line in _VALID_CODE.splitlines() if "phase = PluginPhase" not in line
    )
    check = contract_check(code, plan)
    assert check.ok, check.msg


def test_contract_check_rejects_syntax_error():
    plan = _make_plan()
    check = contract_check(_SYNTAX_BROKEN_CODE, plan)
    assert not check.ok
    assert "syntax error" in check.msg


def test_contract_check_rejects_module_with_no_classes():
    plan = _make_plan()
    check = contract_check("import pandas as pd\nx = 1\n", plan)
    assert not check.ok
    assert "no class definition found" in check.msg


def test_contract_check_rejects_disallowed_import():
    plan = _make_plan()
    code = "import os\n" + _VALID_CODE
    check = contract_check(code, plan)
    assert not check.ok
    assert "disallowed import" in check.msg
    assert "os" in check.msg


def test_contract_check_rejects_dynamic_exec():
    plan = _make_plan()
    code = _VALID_CODE + "\n_x = eval('1')\n"
    check = contract_check(code, plan)
    assert not check.ok
    assert "disallowed call" in check.msg
    assert "eval" in check.msg


def test_contract_check_accepts_allowed_imports():
    plan = _make_plan()
    code = "import numpy as np\nimport math\n" + _VALID_CODE
    check = contract_check(code, plan)
    assert check.ok, check.msg


def test_contract_check_rejects_builtins_reference():
    plan = _make_plan()
    code = _VALID_CODE + "\n_b = __builtins__\n"
    check = contract_check(code, plan)
    assert not check.ok
    assert "__builtins__" in check.msg


def test_contract_check_rejects_open_call():
    plan = _make_plan()
    code = _VALID_CODE + "\n_f = open('/etc/passwd')\n"
    check = contract_check(code, plan)
    assert not check.ok
    assert "disallowed call" in check.msg
    assert "open" in check.msg


def test_contract_check_rejects_relative_import():
    plan = _make_plan()
    code = "from . import something\n" + _VALID_CODE
    check = contract_check(code, plan)
    assert not check.ok
    assert "relative imports" in check.msg


# ---------------------------------------------------------------------------
# PluginImplementer loop tests
# ---------------------------------------------------------------------------


async def test_implementer_happy_path_first_round():
    plan = _make_plan()
    impl = PluginImplementer(model=_stateful_model(_result_args()), max_rounds=3)

    result = await impl.run_implement(plan=plan)

    assert result.rounds_used == 1
    assert result.output.slug == "fancy_indicator"
    assert len(result.llm_calls) == 1


async def test_implementer_recovers_from_syntax_error_in_round_two():
    plan = _make_plan()
    bad = _result_args(code=_SYNTAX_BROKEN_CODE)
    good = _result_args(code=_VALID_CODE)
    impl = PluginImplementer(model=_stateful_model(bad, good), max_rounds=3)

    result = await impl.run_implement(plan=plan)

    assert result.rounds_used == 2
    assert result.output.python_code == _VALID_CODE
    assert len(result.llm_calls) == 2


async def test_implementer_recovers_from_contract_error_in_round_two():
    plan = _make_plan()
    bad = _result_args(code=_WRONG_CLASS_CODE)
    good = _result_args(code=_VALID_CODE)
    impl = PluginImplementer(model=_stateful_model(bad, good), max_rounds=3)

    result = await impl.run_implement(plan=plan)
    assert result.rounds_used == 2


async def test_implementer_exhausts_max_rounds():
    plan = _make_plan()
    bad = _result_args(code=_WRONG_CLASS_CODE)
    impl = PluginImplementer(model=_stateful_model(bad, bad, bad), max_rounds=3)

    with pytest.raises(PluginImplementerError) as exc_info:
        await impl.run_implement(plan=plan)

    err = exc_info.value
    assert err.last_code == _WRONG_CLASS_CODE
    assert err.last_err is not None and "ContractError" in err.last_err
    assert len(err.llm_calls) == 3


async def test_implementer_detects_slug_mismatch_as_gate_fail():
    plan = _make_plan()
    bad = _result_args(slug="totally_different_slug")
    good = _result_args()
    impl = PluginImplementer(model=_stateful_model(bad, good), max_rounds=3)

    result = await impl.run_implement(plan=plan)
    assert result.rounds_used == 2


async def test_implementer_last_err_in_round_two_prompt():
    plan = _make_plan()
    capture: list[Any] = []
    bad = _result_args(code=_SYNTAX_BROKEN_CODE)
    good = _result_args(code=_VALID_CODE)
    impl = PluginImplementer(
        model=_capturing_model(bad, good, capture=capture),
        max_rounds=3,
    )

    await impl.run_implement(plan=plan)

    assert len(capture) == 2
    # round-2 messages should include "Previous attempt failed a gate"
    round2_text = "\n".join(
        part.content
        for msg in capture[1]["messages"]
        for part in getattr(msg, "parts", [])
        if getattr(part, "part_kind", None) == "user-prompt"
    )
    assert "Previous attempt failed a gate" in round2_text
    assert "SyntaxError" in round2_text


async def test_implementer_last_code_in_round_two_prompt():
    plan = _make_plan()
    capture: list[Any] = []
    bad = _result_args(code=_WRONG_CLASS_CODE)
    good = _result_args(code=_VALID_CODE)
    impl = PluginImplementer(
        model=_capturing_model(bad, good, capture=capture),
        max_rounds=3,
    )

    await impl.run_implement(plan=plan)

    round2_text = "\n".join(
        part.content
        for msg in capture[1]["messages"]
        for part in getattr(msg, "parts", [])
        if getattr(part, "part_kind", None) == "user-prompt"
    )
    assert "Previous code (verbatim)" in round2_text
    assert "TotallyDifferentName" in round2_text


async def test_implementer_respects_settings_max_rounds(monkeypatch):
    """Default max_rounds comes from settings.plugin_impl_max_rounds."""
    from fwbg_agents.config import settings

    monkeypatch.setattr(settings, "plugin_impl_max_rounds", 2)
    plan = _make_plan()
    bad = _result_args(code=_WRONG_CLASS_CODE)
    # Default max_rounds → settings.plugin_impl_max_rounds == 2
    impl = PluginImplementer(model=_stateful_model(bad, bad))
    assert impl.max_rounds == 2

    with pytest.raises(PluginImplementerError) as exc_info:
        await impl.run_implement(plan=plan)

    assert len(exc_info.value.llm_calls) == 2


def test_implementer_model_resolves_from_settings(monkeypatch):
    from fwbg_agents.config import settings

    monkeypatch.setattr(settings, "plugin_implementer_model", "claude-haiku-4-5-20251001")
    m = implementer_model()
    assert m.model_name == "claude-haiku-4-5-20251001"


def test_render_implementer_prompt_first_round_no_feedback():
    plan = _make_plan()
    out = _render_implementer_prompt(plan=plan, last_code=None, last_err=None, round_idx=1)
    assert "Round 1" in out
    assert "PluginPlan" in out
    assert "Previous attempt" not in out


def test_render_implementer_prompt_later_round_includes_feedback():
    plan = _make_plan()
    out = _render_implementer_prompt(
        plan=plan,
        last_code="class X: pass",
        last_err="ContractError: class FancyIndicator not found",
        round_idx=2,
    )
    assert "Round 2" in out
    assert "ContractError" in out
    assert "class X: pass" in out
