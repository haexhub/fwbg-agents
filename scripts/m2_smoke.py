"""M2 smoke: drive a strategy through the lifecycle against the real
`data/state.db`, then exercise the read-only endpoints in-process.

Idempotent — re-runnable; uses a unique slug per run.
"""

import asyncio
from datetime import UTC, datetime

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from fwbg_agents.main import app
from fwbg_agents.orchestrator.lifecycle import (
    InvalidTransition,
    strategy_dir,
    transition_strategy,
)
from fwbg_agents.persistence.database import SessionLocal
from fwbg_agents.persistence.models import Strategy, StrategyState, StrategyTag


async def main() -> None:
    now = datetime.now(UTC)
    slug = f"m2_smoke_{now.strftime('%Y%m%d_%H%M%S')}"

    async with SessionLocal() as session:
        s = Strategy(
            slug=slug,
            current_state=StrategyState.PROPOSED.value,
            asset_class="INDEX",
            strategy_family="ORB",
            created_at=now,
            updated_at=now,
        )
        session.add(s)
        await session.commit()
        await session.refresh(s)
        session.add(StrategyTag(strategy_id=s.id, tag="smoke-test"))
        await session.commit()
        print(f"created strategy id={s.id} slug={s.slug} state={s.current_state}")

        # Happy path: proposed → backtested
        await transition_strategy(session, s, StrategyState.BACKTESTED, reason="smoke")
        print(f"after backtested: state={s.current_state}, dir exists={strategy_dir(slug).is_dir()}")

        # Invalid skip (proposed → live blocked by edge table; we're already at
        # backtested, so test the equivalent: backtested → live should fail).
        try:
            await transition_strategy(
                session, s, StrategyState.LIVE_TRADING, reason="bad", payload={"human_approval": True}
            )
        except InvalidTransition as e:
            print(f"invalid skip rejected as expected: {e}")

        # backtested → paper (no criteria YAML for INDEX present → pass-through)
        await transition_strategy(
            session, s, StrategyState.PAPER_TRADING, reason="metrics good",
            payload={"backtest_metrics": {"sharpe": 1.8, "mc_pvalue": 0.02,
                                          "profit_factor": 1.7, "min_trades": 350,
                                          "max_drawdown": 0.18}},
        )
        print(f"after paper_trading: state={s.current_state}")

        # paper → live requires human_approval
        try:
            await transition_strategy(session, s, StrategyState.LIVE_TRADING, reason="auto-promote")
        except InvalidTransition as e:
            print(f"auto-promote rejected: {e}")

        await transition_strategy(
            session, s, StrategyState.LIVE_TRADING, reason="human ok",
            payload={"human_approval": True},
        )
        print(f"after live_trading: state={s.current_state}")

        strategy_id = s.id

    # Read back via the API.
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        r = await client.get("/strategies")
        slugs = [s["slug"] for s in r.json()["strategies"]]
        print(f"GET /strategies -> {len(slugs)} rows, includes {slug}: {slug in slugs}")

        r = await client.get(f"/strategies/{strategy_id}")
        body = r.json()
        print(f"GET /strategies/{strategy_id} -> state={body['strategy']['current_state']}, "
              f"tags={body['strategy']['tags']}, transitions={len(body['transitions'])}")
        for t in body["transitions"]:
            print(f"  {t['from_state']} → {t['to_state']} ({t['reason']!r})")

        r = await client.get("/plugins")
        print(f"GET /plugins -> {len(r.json()['plugins'])} rows")


if __name__ == "__main__":
    asyncio.run(main())
