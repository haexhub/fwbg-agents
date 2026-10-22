"""Tests for the per-lineage research card and the run note (Plan 028 D1/D2).

A fake fwbg client stands in for POST /api/research; everything else (DB rows,
hypothesis/sidecar files, the frozen lineage boundary) is real, in a temp dir.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from fwbg_agents.orchestrator import research_card
from fwbg_agents.orchestrator.lifecycle import strategy_dir
from fwbg_agents.orchestrator.lineage_boundary import boundary_path
from fwbg_agents.orchestrator.research_card import (
    NOTE_MAX_CHARS,
    ResearchCardError,
    ensure_research_id,
    run_note,
)
from fwbg_agents.persistence.database import Base
from fwbg_agents.persistence.models import Strategy, StrategyState

CUTOFF = "2024-06-01"


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    from fwbg_agents.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path / "agents_data")

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/card.db", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    yield Session
    await engine.dispose()


class _CardFake:
    """Records create_research_card calls and answers with sequential card ids.

    With ``hold=True`` the first call parks on ``release`` until someone sets
    it; a second call sets it itself, so callers that skipped mutual exclusion
    both finish and show up as two calls. ``response`` replaces the generated
    answer.
    """

    def __init__(
        self,
        *,
        warning: str | None = None,
        hold: bool = False,
        response: dict[str, Any] | None = None,
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self._warning = warning
        self._hold = hold
        self._response = response
        self.release = asyncio.Event()

    async def create_research_card(
        self, *, title: str, tags: list[str], hypothesis: str, mechanism: str, cutoff: str
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "title": title,
                "tags": tags,
                "hypothesis": hypothesis,
                "mechanism": mechanism,
                "cutoff": cutoff,
            }
        )
        if self._hold:
            if len(self.calls) > 1:
                self.release.set()
            await self.release.wait()
        if self._response is not None:
            return self._response
        response: dict[str, Any] = {"card": {"id": f"S-{len(self.calls):04d}", "title": title}}
        if self._warning is not None:
            response["warning"] = self._warning
        return response


class _ContentionLock(asyncio.Lock):
    """An ``asyncio.Lock`` that sets ``contended`` when a second caller waits for it."""

    def __init__(self, contended: asyncio.Event) -> None:
        super().__init__()
        self._contended = contended

    async def acquire(self) -> bool:
        if self.locked():
            self._contended.set()
        return await super().acquire()


async def _make_strategy(session, slug: str, *, parent_id: int | None = None) -> Strategy:
    now = datetime.now(UTC)
    s = Strategy(
        slug=slug,
        current_state=StrategyState.PROPOSED.value,
        iteration_count=1,
        parent_strategy_id=parent_id,
        asset_class="FOREX",
        strategy_family="ORB",
        metadata_json={"asset_specific": False},
        created_at=now,
        updated_at=now,
    )
    session.add(s)
    await session.commit()
    return s


def _write_hypothesis(slug: str, **overrides: Any) -> None:
    # Only the fields the card needs, plus a free-text family as in files
    # written before the family vocabulary existed: such files must still work.
    # An override of None drops the field.
    payload: dict[str, Any] = {
        "title": "Opening range breakout",
        "strategy_family": "ORB",
        "tags": ["breakout", "session_open"],
        "hypothesis": "Early-session ranges break in the direction of the day.",
        "edge_mechanism": "Overnight order imbalance resolves after the open.",
    }
    payload.update(overrides)
    payload = {key: value for key, value in payload.items() if value is not None}
    path = strategy_dir(slug) / "iteration_001" / "hypothesis.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))


def _freeze_boundary(root_slug: str) -> None:
    path = boundary_path(root_slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"data_end": CUTOFF}))


def _write_recommendation(slug: str, payload: dict[str, Any]) -> None:
    path = strategy_dir(slug) / "iteration_001" / "analyst_recommendation.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))


async def _load(session, strategy_id: int) -> Strategy:
    return (await session.execute(select(Strategy).where(Strategy.id == strategy_id))).scalar_one()


async def test_creates_card_once_and_stores_it_on_the_root(db):
    async with db() as session:
        root = await _make_strategy(session, "orb_root")
    _write_hypothesis("orb_root")
    _freeze_boundary("orb_root")
    fake = _CardFake()

    async with db() as session:
        first = await ensure_research_id(session, await _load(session, root.id), fake)
    async with db() as session:
        second = await ensure_research_id(session, await _load(session, root.id), fake)
        stored = await _load(session, root.id)

    assert first == second == "S-0001"
    assert fake.calls == [
        {
            "title": "Opening range breakout",
            "tags": ["breakout", "session_open"],
            "hypothesis": "Early-session ranges break in the direction of the day.",
            "mechanism": "Overnight order imbalance resolves after the open.",
            "cutoff": CUTOFF,
        }
    ]
    assert stored.metadata_json == {"asset_specific": False, "research_id": "S-0001"}


async def test_child_reuses_the_roots_card_without_a_call(db):
    async with db() as session:
        root = await _make_strategy(session, "orb_root")
        root.metadata_json = {"research_id": "S-0042"}
        await session.commit()
        child = await _make_strategy(session, "orb_root__it002", parent_id=root.id)
        grandchild = await _make_strategy(session, "orb_root__it003", parent_id=child.id)
    fake = _CardFake()

    async with db() as session:
        research_id = await ensure_research_id(session, await _load(session, grandchild.id), fake)
        stored_child = await _load(session, child.id)

    assert research_id == "S-0042"
    assert fake.calls == []
    assert "research_id" not in stored_child.metadata_json


async def test_concurrent_calls_for_one_lineage_create_one_card(db, monkeypatch):
    async with db() as session:
        root = await _make_strategy(session, "orb_root")
        child = await _make_strategy(session, "orb_root__it002", parent_id=root.id)
    _write_hypothesis("orb_root")
    _freeze_boundary("orb_root")
    fake = _CardFake(hold=True)
    # The card request is held until the other caller waits on the lineage lock
    # (or, without mutual exclusion, makes its own request), so the two calls
    # overlap deterministically.
    lock = _ContentionLock(fake.release)
    monkeypatch.setitem(research_card._card_locks, "orb_root", lock)

    async with db() as session_a, db() as session_b:
        root_a = await _load(session_a, root.id)
        # B keeps the root it loaded before the card existed, so it misses the
        # fast path and depends on the re-check under the lock.
        root_b = await _load(session_b, root.id)
        child_b = await _load(session_b, child.id)
        ids = await asyncio.gather(
            ensure_research_id(session_a, root_a, fake),
            ensure_research_id(session_b, child_b, fake),
        )

    assert ids == ["S-0001", "S-0001"]
    assert len(fake.calls) == 1
    assert root_b.metadata_json["research_id"] == "S-0001"


async def test_pending_metadata_change_on_the_root_survives(db):
    async with db() as session:
        root = await _make_strategy(session, "orb_root")
    _write_hypothesis("orb_root")
    _freeze_boundary("orb_root")

    async with db() as session:
        loaded = await _load(session, root.id)
        loaded.metadata_json = {**loaded.metadata_json, "operator_flag": True}
        await ensure_research_id(session, loaded, _CardFake())
    async with db() as session:
        stored = await _load(session, root.id)

    assert stored.metadata_json == {
        "asset_specific": False,
        "operator_flag": True,
        "research_id": "S-0001",
    }


async def test_response_without_card_id_raises(db):
    async with db() as session:
        root = await _make_strategy(session, "orb_root")
    _write_hypothesis("orb_root")
    _freeze_boundary("orb_root")
    fake = _CardFake(response={"card": {"title": "Opening range breakout"}})

    async with db() as session:
        with pytest.raises(ResearchCardError, match="no card id"):
            await ensure_research_id(session, await _load(session, root.id), fake)
        stored = await _load(session, root.id)

    assert "research_id" not in stored.metadata_json


@pytest.mark.parametrize("content", [b'{"title": "cut off', b"\xff\xfe not utf-8"])
async def test_unparseable_hypothesis_raises_without_a_call(db, content):
    async with db() as session:
        root = await _make_strategy(session, "orb_root")
    path = strategy_dir("orb_root") / "iteration_001" / "hypothesis.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(content)
    _freeze_boundary("orb_root")
    fake = _CardFake()

    async with db() as session:
        with pytest.raises(ResearchCardError, match=r"hypothesis\.json"):
            await ensure_research_id(session, await _load(session, root.id), fake)

    assert fake.calls == []


async def test_missing_hypothesis_raises_without_a_call(db):
    async with db() as session:
        root = await _make_strategy(session, "orb_root")
    _freeze_boundary("orb_root")
    fake = _CardFake()

    async with db() as session:
        with pytest.raises(ResearchCardError, match=r"hypothesis\.json") as excinfo:
            await ensure_research_id(session, await _load(session, root.id), fake)
        stored = await _load(session, root.id)

    assert fake.calls == []
    assert "research_id" not in stored.metadata_json
    # The operator learns how to give this lineage a card, and the creation
    # alternative for new strategies.
    message = str(excinfo.value)
    assert "add a valid" in message
    assert "new strategies can instead be created via POST /strategies with a research_id" in (
        message
    )


async def test_legacy_hypothesis_uses_the_edge_explanation_as_mechanism(db):
    async with db() as session:
        root = await _make_strategy(session, "orb_legacy")
    _write_hypothesis(
        "orb_legacy",
        edge_mechanism=None,
        expected_edge_explanation="Opening auctions leave unfilled orders that drive the trend.",
    )
    _freeze_boundary("orb_legacy")
    fake = _CardFake()

    async with db() as session:
        research_id = await ensure_research_id(session, await _load(session, root.id), fake)

    assert research_id == "S-0001"
    assert [call["mechanism"] for call in fake.calls] == [
        "Opening auctions leave unfilled orders that drive the trend."
    ]


async def test_hypothesis_without_mechanism_raises_without_a_call(db):
    async with db() as session:
        root = await _make_strategy(session, "orb_root")
    _write_hypothesis("orb_root", edge_mechanism="   ", expected_edge_explanation=" ")
    _freeze_boundary("orb_root")
    fake = _CardFake()

    async with db() as session:
        with pytest.raises(ResearchCardError, match="edge_mechanism"):
            await ensure_research_id(session, await _load(session, root.id), fake)

    assert fake.calls == []


async def test_warning_is_logged_and_the_id_stored(db, caplog):
    async with db() as session:
        root = await _make_strategy(session, "orb_root")
    _write_hypothesis("orb_root")
    _freeze_boundary("orb_root")
    fake = _CardFake(warning="card written but not committed")

    with caplog.at_level(logging.WARNING, logger="fwbg_agents.orchestrator.research_card"):
        async with db() as session:
            research_id = await ensure_research_id(session, await _load(session, root.id), fake)
    async with db() as session:
        stored = await _load(session, root.id)

    assert research_id == "S-0001"
    assert stored.metadata_json["research_id"] == "S-0001"
    assert any(
        "S-0001" in r.getMessage() and "not committed" in r.getMessage() for r in caplog.records
    )


async def test_root_note_names_the_hypothesis_title(db):
    async with db() as session:
        root = await _make_strategy(session, "orb_root")
    _write_hypothesis("orb_root", title="Opening   range\nbreakout")

    async with db() as session:
        note = await run_note(session, await _load(session, root.id))

    assert note == "initial backtest: Opening range breakout"


async def test_child_note_carries_the_parents_recommendation(db):
    async with db() as session:
        root = await _make_strategy(session, "orb_root")
        child = await _make_strategy(session, "orb_root__it002", parent_id=root.id)
    _write_recommendation(
        "orb_root",
        {"kind": "tune_params", "reasoning": "Widen the\n\nstop range;  too many whipsaws."},
    )

    async with db() as session:
        note = await run_note(session, await _load(session, child.id))

    assert note == "tune_params from orb_root: Widen the stop range; too many whipsaws."


async def test_child_note_is_truncated_to_one_line(db):
    async with db() as session:
        root = await _make_strategy(session, "orb_root")
        child = await _make_strategy(session, "orb_root__it002", parent_id=root.id)
    _write_recommendation("orb_root", {"kind": "change_exit", "reasoning": "word\n" * 200})

    async with db() as session:
        note = await run_note(session, await _load(session, child.id))

    assert len(note) == NOTE_MAX_CHARS
    assert "\n" not in note
    assert note.startswith("change_exit from orb_root: word word")


async def test_fallback_notes_are_non_empty_without_sources(db):
    async with db() as session:
        root = await _make_strategy(session, "orb_root")
        child = await _make_strategy(session, "orb_root__it002", parent_id=root.id)

    async with db() as session:
        root_note = await run_note(session, await _load(session, root.id))
        child_note = await run_note(session, await _load(session, child.id))

    assert root_note == "initial backtest of orb_root"
    assert child_note == "iteration of orb_root__it002"


async def test_universe_attempt_is_appended_and_kept_when_truncating(db):
    async with db() as session:
        root = await _make_strategy(session, "orb_root")
        child = await _make_strategy(session, "orb_root__it002", parent_id=root.id)
    _write_recommendation("orb_root", {"kind": "tune_params", "reasoning": "x " * 300})

    async with db() as session:
        note = await run_note(
            session,
            await _load(session, child.id),
            attempt_label="asset_class:FOREX",
            assets=["EURUSD", "GBPUSD"],
        )

    assert len(note) <= NOTE_MAX_CHARS
    assert note.startswith("tune_params from orb_root: x x")
    assert note.endswith("; universe asset_class:FOREX: EURUSD, GBPUSD")


async def test_long_asset_list_is_capped_and_the_head_keeps_context(db):
    async with db() as session:
        root = await _make_strategy(session, "orb_root")
        child = await _make_strategy(session, "orb_root__it002", parent_id=root.id)
    _write_recommendation("orb_root", {"kind": "tune_params", "reasoning": "x " * 300})
    assets = [f"SYMBOL{i:02d}" for i in range(50)]

    async with db() as session:
        note = await run_note(
            session, await _load(session, child.id), attempt_label="class:FOREX", assets=assets
        )

    assert len(note) <= NOTE_MAX_CHARS
    assert note.startswith("tune_params from orb_root: x x")
    assert note.endswith("+45 more")


async def test_unreadable_recommendation_falls_back(db):
    async with db() as session:
        root = await _make_strategy(session, "orb_root")
        child = await _make_strategy(session, "orb_root__it002", parent_id=root.id)
    path = strategy_dir("orb_root") / "iteration_001" / "analyst_recommendation.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\xff\xfe not utf-8")

    async with db() as session:
        note = await run_note(session, await _load(session, child.id))

    assert note == "iteration of orb_root__it002"
