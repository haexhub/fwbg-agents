"""Tavily web-search client + quota tracking (M4).

Primary research tool for the Researcher agent (design §10).

Quota: every call is logged in `llm_call` with `model="tavily-search"` so the
existing infra (M3 token tracking) doubles as a Tavily counter. No schema
change needed. `get_quota_usage()` counts within a sliding 30-day window.

Fallback strategy (design §10): Anthropic web_search built-in tool is the
documented fallback, but proxy compatibility is unverified (open question §16).
Brave Search is the secondary fallback. Neither is built in M4 — Tavily-only.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime, timedelta

import httpx
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from fwbg_agents.persistence.models import LlmCall

log = logging.getLogger(__name__)

TAVILY_MODEL_NAME = "tavily-search"
DEFAULT_BASE_URL = "https://api.tavily.com"
DEFAULT_TIMEOUT_SECONDS = 30.0


class TavilyUnavailableError(RuntimeError):
    """Raised when the Tavily API key is not configured."""


class SearchResult(BaseModel):
    url: str
    title: str
    content_snippet: str
    score: float


class TavilyClient:
    def __init__(
        self,
        api_key: str | None,
        base_url: str = DEFAULT_BASE_URL,
        http: httpx.AsyncClient | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self._http = http or httpx.AsyncClient(timeout=timeout)
        self._owns_http = http is None

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def search(
        self,
        query: str,
        *,
        max_results: int = 5,
        session: AsyncSession | None = None,
        agent_run_id: int | None = None,
    ) -> list[SearchResult]:
        if not self.api_key:
            raise TavilyUnavailableError("TAVILY_API_KEY is not set")

        body = {
            "api_key": self.api_key,
            "query": query,
            "max_results": max_results,
            "search_depth": "basic",
        }
        start = time.monotonic()
        response = await self._http.post(f"{self.base_url}/search", json=body)
        elapsed_ms = int((time.monotonic() - start) * 1000)
        response.raise_for_status()
        data = response.json()

        results: list[SearchResult] = []
        for raw in data.get("results", []):
            try:
                results.append(
                    SearchResult(
                        url=raw["url"],
                        title=raw["title"],
                        content_snippet=raw["content"],
                        score=float(raw["score"]),
                    )
                )
            except (KeyError, TypeError, ValueError) as exc:
                log.warning("tavily: skipping malformed result %s: %s", raw, exc)
                continue

        if session is not None and agent_run_id is not None:
            await self._log_quota(session, agent_run_id, elapsed_ms)

        return results

    @staticmethod
    async def _log_quota(session: AsyncSession, agent_run_id: int, latency_ms: int) -> None:
        try:
            session.add(
                LlmCall(
                    agent_run_id=agent_run_id,
                    model=TAVILY_MODEL_NAME,
                    input_tokens=0,
                    output_tokens=0,
                    latency_ms=latency_ms,
                    created_at=datetime.now(UTC),
                )
            )
            await session.commit()
        except Exception as exc:
            log.warning("tavily: failed to log quota row: %s", exc)


async def get_quota_usage(
    session: AsyncSession,
    *,
    window_days: int = 30,
) -> int:
    """Count Tavily calls in the trailing window."""
    cutoff = datetime.now(UTC) - timedelta(days=window_days)
    count = (
        await session.execute(
            select(func.count(LlmCall.id)).where(
                LlmCall.model == TAVILY_MODEL_NAME,
                LlmCall.created_at >= cutoff,
            )
        )
    ).scalar_one()
    return int(count or 0)
