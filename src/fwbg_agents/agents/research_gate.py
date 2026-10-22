"""fwbg's research gate refusals of a run start (Plan 028 D5).

fwbg runs its research-register preflight inside ``POST /api/runs/start`` and
answers a refusal with an HTTP error. This module turns those answers into
``ResearchGateRefusedError`` so the Runner can tell a refusal, which ends one
universe attempt, from any other fwbg error.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from fwbg_agents.agents.runner_errors import RunnerError
from fwbg_agents.tools.fwbg_client import FwbgClientError


@dataclass(frozen=True)
class GateDenial:
    """One reason fwbg's research gate gave for refusing a run."""

    code: str
    message: str


class ResearchGateRefusedError(RunnerError):
    """fwbg's research register refused a run start.

    Covers a gate refusal (409/422 with ``denials``), a preflight that failed
    before the gate (422 with ``output``) and a preflight timeout (504). Being
    a RunnerError, it ends only the current universe attempt: the next one
    has another fingerprint.
    """

    def __init__(
        self,
        status: int,
        *,
        detail: str,
        denials: list[GateDenial] | None = None,
        output: str | None = None,
    ):
        self.status = status
        self.detail = detail
        self.denials = denials or []
        self.output = output
        message = f"fwbg research gate ({status}): {detail}"
        if self.denials:
            message += f" [{', '.join(d.code for d in self.denials)}]"
            message += "; " + "; ".join(d.message for d in self.denials)
        if output:
            message += f"; output: {' '.join(output.split())}"
        super().__init__(message)


def research_gate_refusal(exc: FwbgClientError) -> ResearchGateRefusedError | None:
    """Translate a /api/runs/start error into a refusal, or None if it is none.

    fwbg's refusal body is ``{"detail": {"message", "denials" | "output"}}``.
    A 409/422 of another shape (e.g. FastAPI's request validation, whose
    ``detail`` is a list) stays a client error.
    """
    try:
        body = json.loads(exc.body)
    except ValueError:
        body = None
    detail = body.get("detail") if isinstance(body, dict) else None
    if exc.status == 504:
        return ResearchGateRefusedError(
            504, detail=detail if isinstance(detail, str) else "research preflight timed out"
        )
    if exc.status not in (409, 422) or not isinstance(detail, dict):
        return None
    message = detail.get("message")
    if not isinstance(message, str):
        return None
    raw_denials = detail.get("denials")
    if isinstance(raw_denials, list):
        denials = [
            GateDenial(code=str(d.get("code") or "unknown"), message=str(d.get("message", "")))
            for d in raw_denials
            if isinstance(d, dict)
        ]
        return ResearchGateRefusedError(exc.status, detail=message, denials=denials)
    output = detail.get("output")
    return ResearchGateRefusedError(
        exc.status, detail=message, output=output if isinstance(output, str) else None
    )
