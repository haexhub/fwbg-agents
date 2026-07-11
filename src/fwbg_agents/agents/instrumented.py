"""Instrumented LLM run: live tool-call events + full transcript persistence.

Wraps a pydantic-ai agent run so every invocation (a) emits tool calls/results
onto the run's timeline as each model turn completes (``llm_tool_call`` /
``llm_tool_result``) and (b) writes the complete message history to disk as a
pydantic-ai-native JSON transcript for later inspection.

Uses ``Agent.iter()`` rather than ``Agent.run(event_stream_handler=...)``:
``event_stream_handler`` forces the model into *streaming* mode, which the
non-streaming ``FunctionModel`` used across the test suite cannot satisfy
("FunctionModel must receive a stream_function"). ``iter()`` walks the run graph
node-by-node without streaming, so it works with every model — this is the
fallback the plan (STOP condition #1) pre-authorises.

Verified against pydantic-ai 2.0.0: ``CallToolsNode.model_response.parts`` carry
the ``ToolCallPart``s (``part_kind == "tool-call"``, ``tool_name`` +
``args_as_json_str()``); the following ``ModelRequestNode.request.parts`` carry
the ``ToolReturnPart``s (``part_kind == "tool-return"``, ``tool_name`` +
``content``); ``run.result.all_messages()`` + ``.usage`` are intact.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic_ai import Agent
from pydantic_ai.agent import AgentRunResult
from pydantic_ai.messages import ModelMessagesTypeAdapter

from fwbg_agents.run_events import emit_run_event, run_dir

log = logging.getLogger(__name__)

# Per-event payload cap (chars). Keeps SSE volume + queue pressure bounded;
# the full untruncated exchange is always available in the on-disk transcript.
_TRUNC = 2048

# Default name pydantic-ai gives the structured-output tool. Skipped in the
# timeline — the final answer is rendered from the transcript, not as a tool call.
_OUTPUT_TOOL = "final_result"


def _truncate(text: str, limit: int = _TRUNC) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"… [{len(text) - limit} more chars]"


def persist_transcript(
    agent_run_id: int,
    result: Any,
    *,
    round_idx: int = 1,
    model_name: str = "unknown",
) -> None:
    """Persist a completed run's transcript + emit ``llm_round_done``.

    For runs driven without the live event handler — e.g. a synchronous
    ``agent.run_sync()`` agent with no tools (paper_analyst): writes
    ``transcript_<round>.json`` and emits ``llm_round_done``. Errors are logged,
    never raised.
    """
    try:
        d = run_dir(agent_run_id)
        d.mkdir(parents=True, exist_ok=True)
        (d / f"transcript_{round_idx:03d}.json").write_bytes(
            ModelMessagesTypeAdapter.dump_json(result.all_messages())
        )
    except OSError as exc:
        log.warning("run %s: failed to write transcript: %s", agent_run_id, exc)
    usage = getattr(result, "usage", None)
    emit_run_event(
        agent_run_id,
        "llm_round_done",
        round=round_idx,
        model=model_name,
        input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
        output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
    )


async def run_instrumented[OutputT](
    agent: Agent[Any, OutputT],
    user_prompt: str,
    *,
    agent_run_id: int,
    round_idx: int = 1,
) -> AgentRunResult[OutputT]:
    # OutputT preserves the agent's structured output type through the wrapper
    # so callers keep `result.output: <their OutputType>` (not str).
    """Run ``agent`` with live tool-call events + transcript persistence.

    Emits ``llm_tool_call`` / ``llm_tool_result`` per tool invocation, writes
    ``transcript_<round>.json`` after completion, then emits ``llm_round_done``
    with the model name + token usage. Exceptions from the run propagate
    unchanged; telemetry/transcript errors are logged, never raised.
    """
    async with agent.iter(user_prompt) as run:
        async for node in run:
            try:
                # Tool CALLS live on the model response of a CallToolsNode.
                resp = getattr(node, "model_response", None)
                if resp is not None:
                    for part in resp.parts:
                        if (
                            getattr(part, "part_kind", None) == "tool-call"
                            and part.tool_name != _OUTPUT_TOOL
                        ):
                            emit_run_event(
                                agent_run_id,
                                "llm_tool_call",
                                round=round_idx,
                                tool_name=part.tool_name,
                                args=_truncate(part.args_as_json_str()),
                            )
                # Tool RESULTS come back as request parts on the next ModelRequestNode.
                req = getattr(node, "request", None)
                if req is not None:
                    for part in getattr(req, "parts", []):
                        if getattr(part, "part_kind", None) == "tool-return":
                            emit_run_event(
                                agent_run_id,
                                "llm_tool_result",
                                round=round_idx,
                                tool_name=part.tool_name,
                                result=_truncate(str(part.content)),
                            )
            except Exception:  # telemetry must never abort a live agent run
                log.exception("run %s: failed to emit tool event", agent_run_id)
    result = run.result
    if result is None:  # defensive: a completed iter always sets .result
        raise RuntimeError(f"agent run {agent_run_id} produced no result")

    # Persist the full pydantic-ai message history (system prompt, tool calls,
    # tool results, final structured output) as a native JSON transcript.
    try:
        d = run_dir(agent_run_id)
        d.mkdir(parents=True, exist_ok=True)
        (d / f"transcript_{round_idx:03d}.json").write_bytes(
            ModelMessagesTypeAdapter.dump_json(result.all_messages())
        )
    except OSError as exc:
        log.warning("run %s: failed to write transcript: %s", agent_run_id, exc)

    usage = result.usage
    emit_run_event(
        agent_run_id,
        "llm_round_done",
        round=round_idx,
        model=getattr(getattr(agent, "model", None), "model_name", "unknown"),
        input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
        output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
    )
    return result
