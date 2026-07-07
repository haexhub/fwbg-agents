"""Central classifier turning raw API/LLM/HTTP exceptions into clear messages.

Every agent used to store ``str(exc)`` straight into ``AgentRun.error``, so an
API failure surfaced in the UI as the opaque Anthropic SDK text ("Request timed
out or interrupted. This could be due to a network timeout..."). This module
maps the known API/transport exceptions — Anthropic (raw SDK *and* via
pydantic-ai's ``ModelHTTPError``), httpx/asyncio transport errors, and the fwbg
backtest client — to short, categorized English messages.
"""

from __future__ import annotations

import asyncio

import httpx
from pydantic_ai.exceptions import ModelHTTPError

from fwbg_agents.tools.fwbg_client import FwbgClientError

try:  # anthropic is a hard dependency, but stay defensive.
    import anthropic
except ImportError:  # pragma: no cover
    anthropic = None


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
    if anthropic is not None and isinstance(exc, anthropic.APIStatusError):
        status = getattr(exc, "status_code", None)
        if status is None:
            status = getattr(getattr(exc, "response", None), "status_code", None)
        body = getattr(exc, "message", "") or ""
        extra = getattr(exc, "body", None)
        if extra:
            body = f"{body} {extra}".strip()
        return status, body
    return None, ""


def _describe_status(status: int, body: str) -> str:
    low = (body or "").lower()
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
        if (
            "prompt is too long" in low
            or "context length" in low
            or "context window" in low
            or "maximum context" in low
            or "too many tokens" in low
        ):
            return "Context window exceeded — the prompt is too large for the model."
        if "credit balance" in low or "insufficient" in low:
            return "Anthropic credit balance exhausted."
        return f"Anthropic API rejected the request (HTTP 400){_snippet(body)}."
    if 500 <= status < 600:
        return f"Anthropic API server error (HTTP {status}). Retry later."
    return f"Anthropic API error (HTTP {status}){_snippet(body)}."


def describe_api_error(exc: BaseException) -> str | None:
    """Return a clear, human-readable message for a known API/transport error,
    or None if `exc` is not an API error (caller should fall back to str(exc))."""
    # fwbg backtest HTTP API.
    if isinstance(exc, FwbgClientError):
        return f"fwbg API error (HTTP {exc.status}){_snippet(exc.body)}."

    # Transport-level timeouts (check before generic connection errors, since
    # httpx.ConnectTimeout / anthropic.APITimeoutError are also connection errors).
    timeout_types: tuple[type[BaseException], ...] = (
        httpx.TimeoutException,
        asyncio.TimeoutError,
    )
    if anthropic is not None:
        timeout_types += (anthropic.APITimeoutError,)
    if isinstance(exc, timeout_types):
        return "Network timeout reaching the Anthropic API. Retry later."

    connection_types: tuple[type[BaseException], ...] = (httpx.ConnectError,)
    if anthropic is not None:
        connection_types += (anthropic.APIConnectionError,)
    if isinstance(exc, connection_types):
        return "Network connection error reaching the Anthropic API. Retry later."

    # HTTP status errors from Anthropic (raw SDK or wrapped by pydantic-ai).
    status, body = _anthropic_status_body(exc)
    if status is not None:
        return _describe_status(status, body)

    return None
