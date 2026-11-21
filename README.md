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

### fwbg backend requirements

- **Plugin adoption needs `FWBG_ALLOW_PLUGIN_UPLOAD=1` on the fwbg backend.**
  Verified plugins are registered through `POST /api/plugins`. fwbg disables
  that endpoint by default (it executes uploaded code) and answers 403 until
  the flag is set in the *backend's* environment (not this service's). Without
  it, plugin evaluation still completes, but registration fails (best-effort)
  and the `plugin_registration_failed` run event and the log name the flag.
- **fwbg-premium is optional.** It is a separately installed package that fwbg
  discovers through the `fwbg.plugin_packages` entry point. This service reads
  plugins only through the fwbg API; `scripts/backfill_plugin_specs.py` (which
  reads plugin sources off disk) resolves the package through the same entry
  point and covers the core plugins only when it is not installed in the
  environment the script runs in.

## Development

```bash
uv run pytest                # tests
uv run ruff check .          # lint
uv run ruff format .         # format
uv run mypy src              # type-check
```
