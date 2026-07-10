"""Paper-criteria loader + evaluator (M6b Task 3).

Parallel to `lifecycle.check_backtest_criteria` but operating on the
already-typed `PaperTradeSummary` (no fwbg disk read here — Task 4 of M6b
wires the reader call into the orchestrator).

Concrete copy of the comparator from `lifecycle._eval_comparator` — see
locked decision (N): concrete-before-generic. We do NOT extract a shared
helper yet; a third evaluator (M7 live-trading risk gates) is the trigger
to consolidate.

Contract notes:
  - `load_paper_criteria` raises `FileNotFoundError` for unknown
    `asset_class` (NOT pass-through). Callers that want pass-through
    behaviour (e.g. orchestrator/paper_flow.py in Task 5) must catch the
    exception and treat it as "gate open".
  - `evaluate_paper_criteria` honors `required_all` and `hard_blockers`
    sections only. `required_any` (M2-style group-OR) is intentionally
    NOT supported in M6b — it can be added later if a paper-criteria
    YAML ever needs it.
  - Missing-metric failures are reported as
    ``"<metric>: missing from summary"`` — NOT silently passed.
  - Paper-criteria YAMLs MUST NOT use ``_``-prefix keys at the rule
    level: those are skipped (mirrors M2 ``check_backtest_criteria``),
    so a typo like ``_sharpe_paper:`` would silently no-op.
"""

from __future__ import annotations

import operator
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from fwbg_agents.tools.fwbg_paper_reader import PaperTradeSummary

# parents[3]: src/fwbg_agents/orchestrator/criteria_paper.py -> repo root
_DEFAULT_DIR = Path(__file__).resolve().parents[3] / "data" / "criteria" / "paper"


@dataclass
class CriteriaEvalResult:
    """Result of evaluating a strategy against paper-trading criteria."""

    passed: bool
    failures: list[str]


def load_paper_criteria(
    asset_class: str, *, criteria_dir: Path | None = None
) -> dict[str, Any]:
    """Load `<criteria_dir>/<asset_class.lower()>.yaml`.

    Defaults `criteria_dir` to `<repo_root>/data/criteria/paper/`.
    Raises `FileNotFoundError` if no YAML exists for the asset class.
    """
    base = criteria_dir or _DEFAULT_DIR
    path = base / f"{asset_class.lower()}.yaml"
    if not path.exists():
        raise FileNotFoundError(
            f"no paper-criteria YAML for asset_class={asset_class!r}: {path}"
        )
    return yaml.safe_load(path.read_text()) or {}


# Concrete copy of lifecycle._eval_comparator — see locked decision (N):
# concrete-before-generic. Extract to a shared module only when a 3rd
# evaluator appears (M7 live-trading risk gates).
#
# Ordering matters: longer ops first so '>=' is matched before '>' (and
# '<=' before '<', '==' / '!=' before '<' / '>'). Mirrors M2's
# `_OPERATORS` shape in lifecycle.py.
_OPERATORS: list[tuple[str, Callable[[float, float], bool]]] = [
    (">=", operator.ge),
    ("<=", operator.le),
    ("==", operator.eq),
    ("!=", operator.ne),
    (">", operator.gt),
    ("<", operator.lt),
]


def _eval_comparator(metric: str, value: float, expr: str) -> bool:
    """Evaluate a metric value against a threshold comparator expression."""
    expr = expr.strip()
    for op, fn in _OPERATORS:
        if expr.startswith(op):
            threshold = float(expr[len(op) :].strip())
            return fn(value, threshold)
    raise ValueError(f"unparseable comparator: {expr!r}")


def evaluate_paper_criteria(
    summary: PaperTradeSummary, criteria: dict[str, Any]
) -> CriteriaEvalResult:
    """Evaluate a `PaperTradeSummary` against a paper-criteria dict.

    Passes iff every `required_all` AND every `hard_blockers` rule passes.
    Missing metrics in the summary are counted as failures.
    """
    metrics = summary.model_dump()
    failures: list[str] = []
    for section in ("required_all", "hard_blockers"):
        for entry in criteria.get(section, []) or []:
            for metric, expr in entry.items():
                if metric.startswith("_"):
                    continue
                if metric not in metrics or metrics[metric] is None:
                    failures.append(f"{metric}: missing from summary")
                    continue
                ok = _eval_comparator(metric, float(metrics[metric]), expr)
                if not ok:
                    failures.append(f"{metric}: {metrics[metric]} fails '{expr}'")
    return CriteriaEvalResult(passed=not failures, failures=failures)
