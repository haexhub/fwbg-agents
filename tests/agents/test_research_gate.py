"""Translation of fwbg's /api/runs/start errors into research gate refusals.

fwbg answers a gate refusal with 409/422 ``{"detail": {"message", "denials"}}``,
a preflight that failed before the gate with 422 ``{"detail": {"message",
"output"}}`` and a preflight timeout with 504. Everything else stays a plain
client error (None here).
"""

from __future__ import annotations

import json

import pytest

from fwbg_agents.agents.research_gate import GateDenial, research_gate_refusal
from fwbg_agents.agents.runner import RunnerError
from fwbg_agents.tools.fwbg_client import FwbgClientError


def _error(status: int, detail: object) -> FwbgClientError:
    return FwbgClientError(status, json.dumps({"detail": detail}))


def test_gate_refusal_keeps_status_codes_and_messages():
    denials = [
        {"code": "duplicate_fingerprint", "status": 409, "message": "same run exists"},
        {"code": "after_cutoff", "status": 422, "message": "ends after the cutoff"},
    ]
    refusal = research_gate_refusal(
        _error(409, {"message": "research gate refused the run", "denials": denials})
    )

    assert refusal is not None
    assert isinstance(refusal, RunnerError)  # ends only the current universe attempt
    assert refusal.status == 409
    assert refusal.denials == [
        GateDenial("duplicate_fingerprint", "same run exists"),
        GateDenial("after_cutoff", "ends after the cutoff"),
    ]
    assert "duplicate_fingerprint, after_cutoff" in str(refusal)


def test_preflight_failure_keeps_its_message_and_output():
    refusal = research_gate_refusal(
        _error(422, {"message": "research preflight failed", "output": "cannot load\nstrategy"})
    )

    assert refusal is not None
    assert refusal.status == 422
    assert refusal.denials == []
    assert refusal.output == "cannot load\nstrategy"
    assert "research preflight failed" in str(refusal)
    assert "cannot load strategy" in str(refusal)  # one line in the AgentRun error


def test_preflight_timeout_is_a_refusal_whatever_the_body():
    refusal = research_gate_refusal(FwbgClientError(504, "<html>gateway timeout</html>"))

    assert refusal is not None
    assert refusal.status == 504
    assert "timed out" in str(refusal)


@pytest.mark.parametrize(
    "error",
    [
        FwbgClientError(500, "Internal Server Error"),
        # FastAPI's own request validation: a list detail, not the gate's shape.
        _error(422, [{"loc": ["body", "note"], "msg": "field required"}]),
        # fwbg's cancel-during-preflight answer: a plain string detail.
        _error(409, "run was cancelled before it started"),
    ],
)
def test_other_errors_are_not_refusals(error):
    assert research_gate_refusal(error) is None
