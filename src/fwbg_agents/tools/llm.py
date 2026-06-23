"""LLM model factory.

Uses pydantic-ai for provider-neutral agent definitions. The default model is
Anthropic Claude routed through haex-claude-proxy (subscription pricing).
Other providers (OpenAI, Gemini) can be plugged in per-agent when cost or
capability tradeoffs justify it.
"""

from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.providers.anthropic import AnthropicProvider

from fwbg_agents.config import settings


def default_model() -> AnthropicModel:
    """Claude via haex-claude-proxy."""
    provider = AnthropicProvider(
        base_url=settings.anthropic_base_url,
        api_key=settings.anthropic_api_key,
    )
    return AnthropicModel(model_name=settings.anthropic_model, provider=provider)


async def ping() -> dict[str, object]:
    """Minimal round-trip to verify the proxy is reachable and routing."""
    from pydantic_ai import Agent

    agent = Agent(default_model(), system_prompt="Reply with exactly one word.")
    result = await agent.run("Reply with the single word: pong")
    text = result.output.strip()
    usage = result.usage()
    return {
        "ok": "pong" in text.lower(),
        "model": settings.anthropic_model,
        "reply": text,
        "input_tokens": usage.request_tokens,
        "output_tokens": usage.response_tokens,
    }
