"""Central classifier turning raw API/LLM/HTTP exceptions into clear messages.

Every agent used to store ``str(exc)`` straight into ``AgentRun.error``, so an
API failure surfaced in the UI as the opaque Anthropic SDK text ("Request timed
out or interrupted. This could be due to a network timeout..."). This module
maps the known API/transport exceptions — Anthropic (raw SDK *and* via
pydantic-ai's ``ModelHTTPError``), httpx transport errors, and the fwbg
backtest client — to short, categorized English messages. Unknown exceptions
fall back to ``str(exc)``, and wrapped errors are classified via their
``__cause__`` chain.
"""

from __future__ import annotations

import anthropic
import httpx
from pydantic_ai.exceptions import ModelHTTPError

from fwbg_agents.tools.fwbg_client import FwbgClientError


def _snippet(body: str | None, limit: int = 160) -> str:
    """Return a ``": <short body>"`` suffix (or "") for appending to a message."""
    s = (body or "").strip().replace("\n", " ")
    if not s:
        return ""
    if len(s) > limit:
        s = s[:limit] + "…"
    return f": {s}"


def _anthropic_status_body(exc: BaseException) -> tuple[int | None, str]:
    """Extract (HTTP status, body text) from an Anthropic/pydantic-ai HTTP error."""
    if isinstance(exc, ModelHTTPError):
        return exc.status_code, "" if exc.body is None else str(exc.body)
    if isinstance(exc, anthropic.APIStatusError):
        return exc.status_code, f"{exc.message or ''} {exc.body or ''}".strip()
    return None, ""


def _describe_status(status: int, body: str) -> str:
    if status == 429:
        return "Anthropic API rate limit reached — too many requests. Retry later."
    if status == 529:
        return "Anthropic API is overloaded. Retry later."
    if status == 401:
        return "Anthropic API authentication failed — invalid API key."
    if status == 403:
        return (
            "Anthropic API permission denied — your API key is not allowed to "
            "use this resource."
        )
    if status == 400:
        low = body.lower()
        if (
            "prompt is too long" in low
            or "context length" in low
            or "context window" in low
            or "maximum context" in low
            or "too many tokens" in low
        ):
            return "Context window exceeded — the prompt is too large for the model."
        if "credit balance" in low:
            return "Anthropic credit balance exhausted."
        return f"Anthropic API rejected the request (HTTP 400){_snippet(body)}."
    if 500 <= status < 600:
        return f"Anthropic API server error (HTTP {status}). Retry later."
    return f"Anthropic API error (HTTP {status}){_snippet(body)}."


def _classify(exc: BaseException) -> str | None:
    """Message for a known API/transport error, or None if unrecognized."""
    # fwbg backtest HTTP API. No tight snippet cap: fwbg 422 validation
    # bodies carry the actionable detail and used to be persisted in full.
    if isinstance(exc, FwbgClientError):
        return f"fwbg API error (HTTP {exc.status}){_snippet(exc.body, limit=1000)}."

    # Anthropic transport errors (APITimeoutError subclasses APIConnectionError,
    # so check it first).
    if isinstance(exc, anthropic.APITimeoutError):
        return "Timeout reaching the Anthropic API. Retry later."
    if isinstance(exc, anthropic.APIConnectionError):
        return "Connection error reaching the Anthropic API. Retry later."

    # Generic transport errors: httpx also talks to the fwbg backend and the
    # search providers, so don't attribute these to Anthropic. Timeouts before
    # connection errors (httpx.ConnectTimeout is both).
    if isinstance(exc, httpx.TimeoutException | TimeoutError):
        return f"Network timeout during an API call{_snippet(str(exc))}. Retry later."
    if isinstance(exc, httpx.ConnectError):
        return (
            f"Network connection error during an API call{_snippet(str(exc))}. "
            "Retry later."
        )

    # HTTP status errors from Anthropic (raw SDK or wrapped by pydantic-ai).
    status, body = _anthropic_status_body(exc)
    if status is not None:
        return _describe_status(status, body)

    return None


def describe_api_error(exc: BaseException) -> str:
    """Return a clear, human-readable message for a known API/transport error,
    walking the ``__cause__`` chain for wrapped errors; falls back to
    ``str(exc)`` (or ``repr(exc)`` if empty) for everything else."""
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        msg = _classify(cur)
        if msg is not None:
            return msg
        cur = cur.__cause__
    return str(exc) or repr(exc)
