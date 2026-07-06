"""FastAPI application entry point."""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from fwbg_agents import __version__
from fwbg_agents.api import (
    agents_config,
    criteria,
    events,
    health,
    plugins,
    research,
    runs,
    secrets,
    strategies,
)
from fwbg_agents.config import settings
from fwbg_agents.orchestrator import auto_runner, run_janitor
from fwbg_agents.persistence.database import engine

logging.basicConfig(level=settings.log_level)
log = logging.getLogger("fwbg_agents")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    log.info("fwbg-agents starting (version=%s)", __version__)
    # A restart mid-run leaves AgentRuns stuck in pending/running, which
    # permanently blocks the auto-runner's single-flight check — clean up
    # before the loop starts.
    await run_janitor.fail_orphaned_runs()
    auto_runner_task = asyncio.create_task(auto_runner.run_loop())
    pipeline_fill_task = asyncio.create_task(auto_runner.pipeline_fill_loop())
    # Periodic backstop for runs that hang while the process stays alive.
    stale_sweep_task = asyncio.create_task(run_janitor.sweep_loop())
    yield
    stale_sweep_task.cancel()
    pipeline_fill_task.cancel()
    auto_runner_task.cancel()
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
app.include_router(strategies.router)
app.include_router(plugins.router)
app.include_router(runs.router)
app.include_router(research.router)
app.include_router(agents_config.router)
app.include_router(secrets.router)
