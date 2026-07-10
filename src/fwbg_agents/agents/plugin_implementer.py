"""M5d PluginImplementer — refinement-loop agent that turns a PluginPlan into
runnable plugin code via the weaker model (default `claude-opus-4-7`,
env-overridable via PLUGIN_IMPLEMENTER_MODEL).

Loop: implement → syntax gate → contract gate (AST static check). On gate
fail, feed back the last code + last error string and try again. After
`settings.plugin_impl_max_rounds` rounds, raise PluginImplementerError.

Pure callable: returns ImplementerRunResult bundle (output + rounds_used +
per-round LlmCallMeta tuple). Orchestrator persists AgentRun/LlmCalls.
"""

from __future__ import annotations

import ast
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError
from pydantic_ai import Agent
from pydantic_ai.exceptions import UnexpectedModelBehavior
from pydantic_ai.models import Model
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.providers.anthropic import AnthropicProvider

from fwbg_agents.agents.plugin_authoring_shared import (
    PluginAuthorResult,
    SyntaxCheck,
    validate_python_syntax,
)
from fwbg_agents.agents.plugin_planner import LlmCallMeta, PluginPlan
from fwbg_agents.config import settings
from fwbg_agents.tools.llm import model_for, prompt_path_for

log = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).parents[3] / "prompts" / "plugin_authoring.md"

# Expected BasePlugin subclass for each PluginPhase value (where one exists in
# fwbg-sdk). Phases without a widely-used base (labeling, model, validation)
# skip the base-class check.
_PHASE_TO_BASE: dict[str, str] = {
    "indicators": "BaseIndicator",
    "preprocessing": "BasePreprocessor",
    "feature_selection": "BaseFeatureSelector",
    "risk_management": "BaseRiskManager",
    "exit_strategies": "BaseExitStrategy",
    "data_loading": "BaseDataLoader",
}


# Interim import/call gate for LLM-authored plugin code. This is NOT a sandbox:
# plugin.py still runs in-process in the evaluator (see SEC-01 / Plan 004 — the
# real fix is subprocess isolation). Until then we only permit the modules
# legitimate plugins actually use (fwbg_sdk base classes + numpy/pandas + a few
# safe stdlib helpers), which blocks the obvious escapes (os, sys, subprocess,
# socket, shutil, pathlib, importlib, ctypes, pickle, …) by omission. The
# rejection message names the allowlist so extending it is self-explaining.
_ALLOWED_IMPORTS: frozenset[str] = frozenset(
    {
        "fwbg_sdk",  # plugin SDK: BaseIndicator / PluginPhase / helpers
        "pandas",
        "numpy",
        "math",
        "typing",
        "__future__",
        "dataclasses",
    }
)

# Builtins that turn otherwise-static code into an arbitrary-code or
# file-access vector. `__builtins__` is rejected as a bare name too, since
# `getattr(__builtins__, "eval")` would sidestep the call check.
_DISALLOWED_CALLS: frozenset[str] = frozenset(
    {"eval", "exec", "compile", "__import__", "open"}
)


@dataclass(frozen=True)
class ContractCheck:
    ok: bool
    msg: str = ""


@dataclass(frozen=True)
class ImplementerRunResult:
    output: PluginAuthorResult
    rounds_used: int
    llm_calls: tuple[LlmCallMeta, ...]


class PluginImplementerError(RuntimeError):
    """The Implementer exhausted its round budget without passing the gates.

    Carries the last attempted code and last gate error for post-mortem; the
    orchestrator stashes both on the AgentRun row.
    """

    def __init__(
        self,
        message: str,
        *,
        last_code: str | None,
        last_err: str | None,
        llm_calls: tuple[LlmCallMeta, ...],
    ) -> None:
        super().__init__(message)
        self.last_code = last_code
        self.last_err = last_err
        self.llm_calls = llm_calls


def implementer_model() -> Model:
    """Build the PluginImplementer's Anthropic model from settings."""
    provider = AnthropicProvider(
        base_url=settings.anthropic_base_url,
        api_key=settings.anthropic_api_key,
    )
    return AnthropicModel(
        model_name=settings.plugin_implementer_model,
        provider=provider,
    )


def _get_class_attr_value(node: ast.ClassDef, name: str) -> ast.expr | None:
    """Find a class-body assignment `name = <expr>` (handles both Assign and
    AnnAssign forms). Returns the value-node, or None."""
    for stmt in node.body:
        if isinstance(stmt, ast.Assign):
            for tgt in stmt.targets:
                if isinstance(tgt, ast.Name) and tgt.id == name:
                    return stmt.value
        elif (
            isinstance(stmt, ast.AnnAssign)
            and isinstance(stmt.target, ast.Name)
            and stmt.target.id == name
            and stmt.value is not None
        ):
            return stmt.value
    return None


def _base_names(node: ast.ClassDef) -> list[str]:
    """Return identifier names of the class's bases (handles bare-name and
    dotted Attribute forms)."""
    names: list[str] = []
    for b in node.bases:
        if isinstance(b, ast.Name):
            names.append(b.id)
        elif isinstance(b, ast.Attribute):
            names.append(b.attr)
    return names


def _check_imports_and_calls(tree: ast.Module) -> str | None:
    """Reject non-allowlisted imports and dynamic-exec/file-access builtins.

    Returns a rejection message, or None when the code is clean. Import roots
    are compared on `name.split(".")[0]` so an allowlisted package covers its
    submodules (e.g. `fwbg_sdk.indicators`)."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root not in _ALLOWED_IMPORTS:
                    return (
                        f"disallowed import: {alias.name!r} — allowed roots: "
                        f"{sorted(_ALLOWED_IMPORTS)} (interim gate pending sandbox)"
                    )
        elif isinstance(node, ast.ImportFrom):
            # module is None for `from . import x`; reject relative imports too.
            root = (node.module or "").split(".")[0]
            if root not in _ALLOWED_IMPORTS:
                return (
                    f"disallowed import: {node.module!r} — allowed roots: "
                    f"{sorted(_ALLOWED_IMPORTS)} (interim gate pending sandbox)"
                )
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in _DISALLOWED_CALLS:
                return (
                    f"disallowed call: {node.func.id}() is not permitted in "
                    "plugin code (interim gate pending sandbox)"
                )
        elif isinstance(node, ast.Name) and node.id == "__builtins__":
            return (
                "disallowed reference: __builtins__ is not permitted in "
                "plugin code (interim gate pending sandbox)"
            )
    return None


def contract_check(code: str, plan: PluginPlan) -> ContractCheck:
    """Static AST contract gate: verify the code defines `plan.class_name`
    inheriting from the expected phase base, with `name = plan.slug` and
    `phase = PluginPhase.<plan.phase>` (or inheriting the phase from a SDK
    base class for phases listed in _PHASE_TO_BASE).
    """
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return ContractCheck(ok=False, msg=f"syntax error in code: {exc}")

    classes = [n for n in tree.body if isinstance(n, ast.ClassDef)]
    if not classes:
        return ContractCheck(ok=False, msg="no class definition found at module top-level")

    target = next((c for c in classes if c.name == plan.class_name), None)
    if target is None:
        names = [c.name for c in classes]
        return ContractCheck(
            ok=False,
            msg=f"class {plan.class_name!r} not found; module defines: {names}",
        )

    expected_base = _PHASE_TO_BASE.get(plan.phase)
    if expected_base is not None:
        bases = _base_names(target)
        if expected_base not in bases:
            return ContractCheck(
                ok=False,
                msg=(
                    f"class {plan.class_name} must inherit from {expected_base} for "
                    f"phase={plan.phase!r}; got bases: {bases}"
                ),
            )

    name_expr = _get_class_attr_value(target, "name")
    if name_expr is None:
        return ContractCheck(
            ok=False,
            msg=f"class {plan.class_name} must set `name = {plan.slug!r}`",
        )
    if not (isinstance(name_expr, ast.Constant) and name_expr.value == plan.slug):
        actual = name_expr.value if isinstance(name_expr, ast.Constant) else ast.unparse(name_expr)
        return ContractCheck(
            ok=False,
            msg=f"class {plan.class_name}.name must equal slug {plan.slug!r}, got {actual!r}",
        )

    # `phase` is optional on the subclass if the chosen base class already
    # sets it (e.g. BaseIndicator hardcodes phase = PluginPhase.INDICATORS).
    phase_expr = _get_class_attr_value(target, "phase")
    if phase_expr is not None:
        if not (
            isinstance(phase_expr, ast.Attribute)
            and isinstance(phase_expr.value, ast.Name)
            and phase_expr.value.id == "PluginPhase"
        ):
            return ContractCheck(
                ok=False,
                msg=(
                    f"class {plan.class_name}.phase must be a `PluginPhase.X` reference, "
                    f"got {ast.unparse(phase_expr)!r}"
                ),
            )
        if phase_expr.attr.lower() != plan.phase:
            return ContractCheck(
                ok=False,
                msg=(
                    f"class {plan.class_name}.phase must be PluginPhase.{plan.phase.upper()}, "
                    f"got PluginPhase.{phase_expr.attr}"
                ),
            )

    import_or_call_err = _check_imports_and_calls(tree)
    if import_or_call_err is not None:
        return ContractCheck(ok=False, msg=import_or_call_err)

    return ContractCheck(ok=True)


def _render_implementer_prompt(
    *,
    plan: PluginPlan,
    last_code: str | None,
    last_err: str | None,
    round_idx: int,
) -> str:
    plan_block = json.dumps(plan.model_dump(), indent=2, default=str)
    prompt = (
        "## Implementation request\n"
        f"Round {round_idx}. Implement the plugin matching the PluginPlan below.\n\n"
        "## PluginPlan\n"
        f"```json\n{plan_block}\n```\n\n"
    )
    if last_code is not None and last_err is not None:
        prompt += (
            "## Previous attempt failed a gate\n"
            f"Gate error: {last_err}\n\n"
            "## Previous code (verbatim)\n"
            f"```python\n{last_code}\n```\n\n"
            "Fix the gate error and re-emit a corrected PluginAuthorResult.\n"
        )
    else:
        prompt += "Emit a PluginAuthorResult now.\n"
    return prompt


class PluginImplementer:
    """Refinement-loop agent: PluginPlan → PluginAuthorResult via N rounds
    bounded by syntax + contract gates.

    Caller (orchestrator) is responsible for AgentRun + per-round LlmCall
    persistence; this class returns telemetry in ImplementerRunResult.
    """

    def __init__(
        self,
        *,
        model: Model | None = None,
        max_rounds: int | None = None,
        prompt_path: Path | None = None,
    ) -> None:
        self.model = model if model is not None else model_for("plugin_implementer")
        self.max_rounds = (
            max_rounds if max_rounds is not None else settings.plugin_impl_max_rounds
        )
        self.prompt_path = prompt_path or prompt_path_for("plugin_implementer", _PROMPT_PATH)

    async def run_implement(self, *, plan: PluginPlan) -> ImplementerRunResult:
        try:
            system_prompt = self.prompt_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise PluginImplementerError(
                f"prompt-doc not readable at {self.prompt_path}: {exc}",
                last_code=None,
                last_err=None,
                llm_calls=(),
            ) from exc

        last_code: str | None = None
        last_err: str | None = None
        llm_calls: list[LlmCallMeta] = []

        for round_idx in range(1, self.max_rounds + 1):
            user_prompt = _render_implementer_prompt(
                plan=plan,
                last_code=last_code,
                last_err=last_err,
                round_idx=round_idx,
            )

            agent: Agent[None, PluginAuthorResult] = Agent(
                self.model,
                output_type=PluginAuthorResult,
                system_prompt=system_prompt,
            )

            t0 = time.monotonic()
            try:
                result = await agent.run(user_prompt)
            except (ValidationError, UnexpectedModelBehavior) as exc:
                latency_ms = int((time.monotonic() - t0) * 1000)
                llm_calls.append(
                    LlmCallMeta(
                        model_name=getattr(self.model, "model_name", "unknown"),
                        input_tokens=0,
                        output_tokens=0,
                        latency_ms=latency_ms,
                    )
                )
                last_err = f"output schema validation failed: {exc}"
                # `last_code` stays whatever it was — the LLM didn't give us new code.
                continue

            latency_ms = int((time.monotonic() - t0) * 1000)
            usage = result.usage
            llm_calls.append(
                LlmCallMeta(
                    model_name=getattr(self.model, "model_name", "unknown"),
                    input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
                    output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
                    latency_ms=latency_ms,
                )
            )

            output = result.output
            code = output.python_code

            syntax: SyntaxCheck = validate_python_syntax(code)
            if not syntax.ok:
                last_code = code
                last_err = f"SyntaxError L{syntax.line}: {syntax.msg}"
                continue

            check = contract_check(code, plan)
            if not check.ok:
                last_code = code
                last_err = f"ContractError: {check.msg}"
                continue

            # Also verify the LLM-emitted slug matches the plan's slug.
            if output.slug != plan.slug:
                last_code = code
                last_err = (
                    f"slug mismatch: plan.slug={plan.slug!r}, "
                    f"output.slug={output.slug!r}"
                )
                continue

            log.info(
                "plugin_implementer.run_implement_ok slug=%s rounds=%d total_latency_ms=%d",
                plan.slug,
                round_idx,
                sum(c.latency_ms for c in llm_calls),
            )
            return ImplementerRunResult(
                output=output,
                rounds_used=round_idx,
                llm_calls=tuple(llm_calls),
            )

        raise PluginImplementerError(
            f"gates still failing after {self.max_rounds} rounds: {last_err}",
            last_code=last_code,
            last_err=last_err,
            llm_calls=tuple(llm_calls),
        )
