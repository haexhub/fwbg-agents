"""Unit tests for the central API-error classifier."""

from __future__ import annotations

import anthropic
import httpx
from pydantic_ai.exceptions import ModelHTTPError

from fwbg_agents.tools.api_errors import describe_api_error
from fwbg_agents.tools.fwbg_client import FwbgClientError


def _model_http_error(status: int, body: str = "") -> ModelHTTPError:
    return ModelHTTPError(status_code=status, model_name="claude-test", body=body)


def _anthropic_response(status: int) -> httpx.Response:
    return httpx.Response(
        status, request=httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    )


def test_rate_limit_429():
    msg = describe_api_error(_model_http_error(429))
    assert msg == "Anthropic API rate limit reached — too many requests. Retry later."


def test_overloaded_529():
    assert describe_api_error(_model_http_error(529)) == (
        "Anthropic API is overloaded. Retry later."
    )


def test_context_window_400():
    exc = _model_http_error(400, "prompt is too long: 250000 tokens > 200000 maximum")
    assert describe_api_error(exc) == (
        "Context window exceeded — the prompt is too large for the model."
    )


def test_credit_balance_400():
    exc = _model_http_error(400, "Your credit balance is too low to access the API")
    assert describe_api_error(exc) == "Anthropic credit balance exhausted."


def test_400_with_insufficient_elsewhere_is_not_credit_balance():
    exc = _model_http_error(400, "insufficient tool_result blocks in message")
    msg = describe_api_error(exc)
    assert "credit balance" not in msg.lower()
    assert "HTTP 400" in msg


def test_generic_400_keeps_status_and_snippet():
    msg = describe_api_error(_model_http_error(400, "some unexpected validation failure"))
    assert "HTTP 400" in msg
    assert "some unexpected validation failure" in msg


def test_auth_401():
    assert describe_api_error(_model_http_error(401)) == (
        "Anthropic API authentication failed — invalid API key."
    )


def test_permission_403():
    assert "permission denied" in describe_api_error(_model_http_error(403)).lower()


def test_server_error_5xx_includes_code():
    assert describe_api_error(_model_http_error(503)) == (
        "Anthropic API server error (HTTP 503). Retry later."
    )


def test_unknown_status_falls_back_to_generic_with_status():
    msg = describe_api_error(_model_http_error(418, "teapot"))
    assert "HTTP 418" in msg
    assert "teapot" in msg


def test_anthropic_sdk_ratelimit_via_status_attr():
    exc = anthropic.RateLimitError(
        "too many requests", response=_anthropic_response(429), body=None
    )
    assert describe_api_error(exc) == (
        "Anthropic API rate limit reached — too many requests. Retry later."
    )


def test_anthropic_sdk_overloaded():
    exc = anthropic.OverloadedError(
        "overloaded", response=_anthropic_response(529), body=None
    )
    assert describe_api_error(exc) == "Anthropic API is overloaded. Retry later."


def test_httpx_timeout_is_not_attributed_to_anthropic():
    msg = describe_api_error(httpx.ReadTimeout("timed out"))
    assert "timeout" in msg.lower()
    assert "anthropic" not in msg.lower()


def test_builtin_timeout():
    msg = describe_api_error(TimeoutError("backtest poll exceeded cap"))
    assert "timeout" in msg.lower()
    assert "backtest poll exceeded cap" in msg


def test_httpx_connect_error_is_not_attributed_to_anthropic():
    msg = describe_api_error(httpx.ConnectError("connection refused"))
    assert "connection" in msg.lower()
    assert "anthropic" not in msg.lower()


def test_anthropic_sdk_timeout():
    exc = anthropic.APITimeoutError(
        request=httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    )
    assert describe_api_error(exc) == "Timeout reaching the Anthropic API. Retry later."


def test_fwbg_client_error_with_body():
    exc = FwbgClientError(503, "backend unavailable")
    assert describe_api_error(exc) == (
        "fwbg API error (HTTP 503): backend unavailable."
    )


def test_fwbg_client_error_without_body():
    assert describe_api_error(FwbgClientError(500, "")) == "fwbg API error (HTTP 500)."


def test_fwbg_long_body_is_kept():
    body = "x" * 600
    msg = describe_api_error(FwbgClientError(422, body))
    assert body in msg


def test_non_api_exception_falls_back_to_str():
    assert describe_api_error(ValueError("x")) == "x"


def test_empty_message_falls_back_to_repr():
    assert describe_api_error(ValueError()) == "ValueError()"


def test_wrapped_api_error_is_classified_via_cause():
    try:
        try:
            raise _model_http_error(429)
        except ModelHTTPError as inner:
            raise RuntimeError(f"planner failed: {inner}") from inner
    except RuntimeError as exc:
        msg = describe_api_error(exc)
    assert msg == "Anthropic API rate limit reached — too many requests. Retry later."


def test_long_body_is_truncated():
    msg = describe_api_error(_model_http_error(418, "y" * 500))
    assert "…" in msg
    assert len(msg) < 300
