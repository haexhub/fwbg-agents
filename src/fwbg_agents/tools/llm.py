"""LLM model factory.

Uses pydantic-ai for provider-neutral agent definitions. The default model is
Anthropic Claude routed through haex-claude-proxy (subscription pricing).
Other providers (OpenAI, Gemini) can be plugged in per-agent when cost or
capability tradeoffs justify it.
"""

from pathlib import Path

from anthropic import AsyncAnthropic
from pydantic_ai.models.anthropic import AnthropicModel, AnthropicModelSettings
from pydantic_ai.providers.anthropic import AnthropicProvider

from fwbg_agents.config import settings

# Claude models selectable per agent via /agents/config. All route through the
# same haex-claude-proxy, so no extra API keys are needed to switch between them.
AVAILABLE_CLAUDE_MODELS: tuple[str, ...] = (
    "claude-opus-4-8",
    "claude-opus-4-7",
    "claude-sonnet-5",
    "claude-sonnet-4-6",
    "claude-haiku-4-5",
)


def _build_model(model_name: str) -> AnthropicModel:
    """Construct an AnthropicModel with project-configured timeout and retry settings."""
    # Own the Anthropic client so we control both the per-request timeout and
    # the retry budget. The SDK default (max_retries=2 = 3 attempts) turned a
    # too-short 120s timeout into ~6min stacked failures on every long Opus
    # call. A generous timeout lets a legitimately long generation finish;
    # llm_max_retries bounds a wedged-proxy hang to a small multiple of it.
    client = AsyncAnthropic(
        base_url=settings.anthropic_base_url,
        api_key=settings.anthropic_api_key,
        timeout=settings.llm_timeout_seconds,
        max_retries=settings.llm_max_retries,
    )
    provider = AnthropicProvider(anthropic_client=client)
    return AnthropicModel(
        model_name=model_name,
        provider=provider,
        settings=AnthropicModelSettings(timeout=settings.llm_timeout_seconds),
    )


def role_default_model(agent_name: str) -> str:
    """Built-in default model for an agent, before any runtime override.

    Preserves the historical per-role split (Planner stronger, Implementer
    weaker); everything else falls back to the global ``anthropic_model``.
    """
    if agent_name == "plugin_planner":
        return settings.plugin_planner_model
    if agent_name == "plugin_implementer":
        return settings.plugin_implementer_model
    return settings.anthropic_model


def model_name_for(agent_name: str) -> str:
    """Effective model name: runtime override if set, else the role default."""
    from fwbg_agents.tools import agent_config

    return agent_config.get_model_override(agent_name) or role_default_model(agent_name)


def model_for(agent_name: str) -> AnthropicModel:
    """Anthropic model for a given agent, honoring its runtime override."""
    return _build_model(model_name_for(agent_name))


def prompt_path_for(agent_name: str, default_path: Path) -> Path:
    """Override persona file if one exists on disk, else the bundled default."""
    from fwbg_agents.tools import agent_config

    override = agent_config.prompt_override_path(agent_name)
    return override if override.is_file() else default_path


def default_model() -> AnthropicModel:
    """Claude via haex-claude-proxy (global default model)."""
    return _build_model(settings.anthropic_model)


async def ping() -> dict[str, object]:
    """Minimal round-trip to verify the proxy is reachable and routing."""
    from pydantic_ai import Agent

    agent = Agent(default_model(), system_prompt="Reply with exactly one word.")
    result = await agent.run("Reply with the single word: pong")
    text = result.output.strip()
    # pydantic-ai 2.0: usage is a property with input_tokens/output_tokens
    # (was a callable returning request_tokens/response_tokens pre-2.0).
    usage = result.usage
    return {
        "ok": "pong" in text.lower(),
        "model": settings.anthropic_model,
        "reply": text,
        "input_tokens": getattr(usage, "input_tokens", 0),
        "output_tokens": getattr(usage, "output_tokens", 0),
    }
