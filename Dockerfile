FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

COPY pyproject.toml uv.lock README.md alembic.ini ./
COPY src/ src/

RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH"

# Persisted via the agents-data volume (sqlite state.db + criteria files)
RUN mkdir -p data

EXPOSE 8421

CMD ["sh", "-c", "alembic upgrade head && uvicorn fwbg_agents.main:app --host 0.0.0.0 --port 8421"]
