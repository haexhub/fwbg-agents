# fwbg-agents

Autonomous agents for the fwbg trading strategy ecosystem. Researches strategies on the web, translates them into fwbg-compatible configs, runs backtests, evaluates results, iterates parameters, deploys promising strategies to paper trading, and after a 3-month observation period to live trading with strict risk controls.

## Architecture

See `docs/2026-06-23-fwbg-agents-design.md` in the fwbg repository for the complete design document.

```
fwbg-dashboard (Nuxt) ── REST + SSE ──► fwbg-agents (this repo)
                                              │
                                              ├─► fwbg HTTP API (backtests)
                                              ├─► haex-claude-proxy (LLM, subscription)
                                              ├─► Tavily API (web research)
                                              └─► IG broker (paper + live)
```

## Status

**M0 — Skeleton.** FastAPI app boots, SQLite is initialized, mock SSE stream works, proxy connection test passes. No agents implemented yet.

See milestones M0–M8 in the design document.

## Quick start

```bash
# Install dependencies
uv sync

# Initialize database
uv run alembic upgrade head

# Run the API server
uv run uvicorn fwbg_agents.main:app --reload --port 8421
```

API docs at `http://localhost:8421/docs`.

## Configuration

Copy `.env.example` to `.env` and fill in:

```bash
ANTHROPIC_BASE_URL=http://localhost:8080   # haex-claude-proxy
TAVILY_API_KEY=...
FWBG_API_URL=http://localhost:8420
```

## Development

```bash
uv run pytest                # tests
uv run ruff check .          # lint
uv run ruff format .         # format
uv run mypy src              # type-check
```
