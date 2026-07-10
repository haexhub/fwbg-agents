"""Per-agent configuration API — model + persona/system-prompt overrides.

Read/write the file-backed overrides (:mod:`fwbg_agents.tools.agent_config`).
Only the LLM-driven roles are configurable; everything routes through the same
haex-claude-proxy, so switching model needs no extra credentials. Changes apply
to the next agent run.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from fwbg_agents.tools import agent_config, llm

router = APIRouter(prefix="/agents/config", tags=["agents-config"])


class AgentConfigUpdate(BaseModel):
    """Payload for PUT /agents/config/{name} — model and prompt overrides."""

    # Provided value replaces the override; empty/None resets to the default.
    # The same payload carries both fields — the UI always submits both.
    model: str | None = None
    prompt: str | None = None


def _view(name: str) -> dict[str, Any]:
    """Build a config view dict for a single configurable agent."""
    return {
        "name": name,
        "model": llm.model_name_for(name),
        "default_model": llm.role_default_model(name),
        "has_model_override": agent_config.get_model_override(name) is not None,
        "prompt": agent_config.effective_prompt(name),
        "default_prompt": agent_config.default_prompt(name),
        "has_prompt_override": agent_config.get_prompt_override(name) is not None,
    }


@router.get("")
def list_agent_configs() -> dict[str, Any]:
    """List configuration for all configurable agents with available models."""
    return {
        "agents": [_view(name) for name in agent_config.CONFIGURABLE_AGENTS],
        "available_models": list(llm.AVAILABLE_CLAUDE_MODELS),
    }


@router.get("/{name}")
def get_agent_config(name: str) -> dict[str, Any]:
    """Retrieve configuration for a single configurable agent by name."""
    if name not in agent_config.CONFIGURABLE_AGENTS:
        raise HTTPException(404, f"unknown configurable agent: {name!r}")
    return _view(name)


@router.put("/{name}")
def put_agent_config(name: str, body: AgentConfigUpdate) -> dict[str, Any]:
    """Update model and/or prompt override for a configurable agent."""
    if name not in agent_config.CONFIGURABLE_AGENTS:
        raise HTTPException(404, f"unknown configurable agent: {name!r}")

    model = (body.model or "").strip()
    if model and model not in llm.AVAILABLE_CLAUDE_MODELS:
        raise HTTPException(422, f"unknown model: {model!r}")
    agent_config.set_model_override(name, model or None)

    # Treat an empty prompt — or one identical to the bundled default — as a
    # reset, so the UI prefilling the default doesn't create a no-op override.
    prompt = body.prompt if body.prompt is not None else ""
    if not prompt.strip() or prompt.strip() == agent_config.default_prompt(name).strip():
        agent_config.set_prompt_override(name, None)
    else:
        agent_config.set_prompt_override(name, prompt)

    return _view(name)
