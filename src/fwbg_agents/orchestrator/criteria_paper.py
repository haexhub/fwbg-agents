"""Paper-criteria loader + evaluator (M6b Task 3).

Parallel to `lifecycle.check_backtest_criteria` but operating on the
already-typed `PaperTradeSummary` (no fwbg disk read here — Task 4 of M6b
wires the reader call into the orchestrator).

Concrete copy of the comparator from `lifecycle._eval_comparator` — see
locked decision (N): concrete-before-generic. We do NOT extract a shared
helper yet; a third evaluator (M7 live-trading risk gates) is the trigger
to consolidate.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from fwbg_agents.tools.fwbg_paper_reader import PaperTradeSummary

_DEFAULT_DIR = Path(__file__).resolve().parents[3] / "data" / "criteria" / "paper"


@dataclass
class CriteriaEvalResult:
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
_OPS = {
    ">=": lambda a, b: a >= b,
    "<=": lambda a, b: a <= b,
    ">": lambda a, b: a > b,
    "<": lambda a, b: a < b,
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
}


def _eval(metric: str, value: float, expr: str) -> tuple[bool, str]:
    expr = expr.strip()
    for op in (">=", "<=", "==", "!=", ">", "<"):
        if expr.startswith(op):
            threshold = float(expr[len(op) :].strip())
            ok = _OPS[op](value, threshold)
            return ok, f"{metric}: {value} {op} {threshold} -> {'pass' if ok else 'fail'}"
    raise ValueError(f"unparseable comparator: {expr!r}")


def evaluate_paper_criteria(
    summary: PaperTradeSummary, criteria: dict[str, Any]
) -> CriteriaEvalResult:
    """Evaluate a `PaperTradeSummary` against a paper-criteria dict.

    Passes iff every `required_all` AND every `hard_blockers` rule passes.
    Missing metrics in the summary are counted as failures.
    """
    metrics = (
        summary.model_dump() if hasattr(summary, "model_dump") else summary.__dict__
    )
    failures: list[str] = []
    for section in ("required_all", "hard_blockers"):
        for entry in criteria.get(section, []) or []:
            for metric, expr in entry.items():
                if metric not in metrics or metrics[metric] is None:
                    failures.append(f"{metric}: missing from summary")
                    continue
                ok, _msg = _eval(metric, float(metrics[metric]), expr)
                if not ok:
                    failures.append(f"{metric}: {metrics[metric]} fails '{expr}'")
    return CriteriaEvalResult(passed=not failures, failures=failures)
