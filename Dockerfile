# ---- builder: resolve + install deps into /app/.venv ----
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

COPY pyproject.toml uv.lock README.md ./
COPY src/ src/

# Cache mount keeps uv's download/build cache OUT of the image layer
# (~390 MB otherwise) while still speeding up rebuilds.
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev

# ---- runtime: slim image with only the venv + app, no uv toolchain ----
FROM python:3.12-slim-bookworm

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

# The venv's editable install references /app/src by absolute path, so the
# source must live at the same location here as in the builder.
COPY --from=builder /app/.venv /app/.venv
COPY src/ src/
# plugin_planner/plugin_implementer load their persona from the repo-root
# prompts/ dir (see tools/agent_config.DEFAULT_PROMPT_PATHS) — without it
# /agents/config 500s and plugin authoring cannot start.
COPY prompts/ prompts/
COPY alembic.ini ./

# Persisted via the agents-data volume (sqlite state.db + criteria files)
RUN mkdir -p data

EXPOSE 8421

CMD ["sh", "-c", "alembic upgrade head && uvicorn fwbg_agents.main:app --host 0.0.0.0 --port 8421"]
