"""Health checks: service liveness + dependency reachability."""

from fastapi import APIRouter
from sqlalchemy import text

from fwbg_agents import __version__
from fwbg_agents.persistence.database import engine
from fwbg_agents.tools.llm import ping as llm_ping

router = APIRouter(prefix="/healthz", tags=["health"])


@router.get("")
async def healthz() -> dict[str, object]:
    """Liveness probe. Always returns 200 if the process is up."""
    return {"status": "ok", "version": __version__}


@router.get("/db")
async def healthz_db() -> dict[str, object]:
    """Verify SQLite is reachable and queryable."""
    try:
        async with engine.connect() as conn:
            result = await conn.execute(text("SELECT 1"))
            row = result.scalar_one()
        return {"status": "ok", "result": row}
    except Exception as e:
        return {"status": "error", "error": str(e)}


@router.get("/proxy")
async def healthz_proxy() -> dict[str, object]:
    """Verify haex-claude-proxy is reachable and the LLM responds."""
    try:
        result = await llm_ping()
        return {"status": "ok", **result}
    except Exception as e:
        return {"status": "error", "error": str(e)}
