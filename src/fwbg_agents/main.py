"""FastAPI application entry point."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from fwbg_agents import __version__
from fwbg_agents.api import criteria, events, health
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

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router)
app.include_router(events.router)
app.include_router(criteria.router)
