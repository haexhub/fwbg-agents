"""FastAPI application entry point."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from fwbg_agents import __version__
from fwbg_agents.api import events, health
from fwbg_agents.config import settings
from fwbg_agents.persistence.database import engine

logging.basicConfig(level=settings.log_level)
log = logging.getLogger("fwbg_agents")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    log.info("fwbg-agents starting (version=%s)", __version__)
    yield
    await engine.dispose()
    log.info("fwbg-agents shut down")


app = FastAPI(
    title="fwbg-agents",
    version=__version__,
    description="Autonomous agents for fwbg strategy research and trading",
    lifespan=lifespan,
)

app.include_router(health.router)
app.include_router(events.router)
