"""Errors of the Runner agent (see agents/runner.py)."""

from __future__ import annotations


class RunnerError(RuntimeError):
    """Raised when every universe attempt fails (fwbg errors / no results)."""


class UnrunnableStrategyError(RunnerError):
    """The strategy cannot produce a meaningful backtest and must not be
    dispatched — e.g. a signal model with no entry-signal source (no
    signal_rules, no model.required_features, no allowed_hours/allowed_days).

    Broadening the universe or retrying cannot help; fwbg would skip every fold
    with an empty feature pool and "complete" in seconds. We short-circuit
    before creating the run so no wasted backtest is dispatched.
    """


class RunnerConfigError(RunnerError):
    """fwbg completed the run but every asset errored on a fixable
    strategy-config problem (e.g. a plugin missing its pipeline dependency).

    Broadening the universe cannot fix such an error — the same pipeline fails
    on every symbol — so the Runner short-circuits and raises this instead of
    exhausting all rungs. It carries the parsed dependency (`dependent` needs
    `dependency` upstream) so the orchestrator can auto-repair by reiteration.
    """

    def __init__(
        self, message: str, *, dependent: str | None = None, dependency: str | None = None
    ):
        super().__init__(message)
        self.dependent = dependent
        self.dependency = dependency
