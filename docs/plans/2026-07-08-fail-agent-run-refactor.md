# fail_agent_run() — ein Helper statt 16 kopierter except-Blöcke

**Status:** Plan (Follow-up aus dem Code-Review zu PR #111)
**Repo:** fwbg-agents, Branch von `develop`, ein PR.

## Problem

Der 4-Zeilen-Block

```python
ar.status = AgentRunStatus.FAILED.value
ar.ended_at = datetime.now(UTC)
ar.error = describe_api_error(exc)
await session.commit()
```

steht nach PR #111 an **16 exception-getriebenen Stellen in 11 Dateien** (plus 5
`_finish_agent_run(status=FAILED, …)`-Aufrufe in `plugin_flow.py`). Die
Fehlerklassifikation ist damit eine **Konvention, die jeder neue except-Block
kennen muss** — genau der Copy-Paste-Drift, den PR #111 behoben hat, entsteht
beim nächsten Flow wieder (dieses Repo hat innerhalb weniger Wochen paper_flow,
plugin_flow, auto_runner und reiterate-with-plugin bekommen). Beleg: noch im
PR #111 selbst emittierte der Researcher rohes `str(exc)` ins Event, während
eine Zeile darüber die klassifizierte Meldung persistiert wurde.

## Ist-Inventar (develop, Stand 2026-07-08)

Exception-getriebene FAILED-Blöcke (= Refactoring-Ziel):

| # | Stelle | Besonderheit |
|---|--------|--------------|
| 1 | `agents/analyst.py:511` | re-raise |
| 2 | `agents/researcher.py:229` | + `event_bus.emit(agent_run_failed)` mit `ar.error`; re-raise |
| 3 | `agents/runner.py:236` | `transient: `-Prefix bei `httpx.TransportError`; re-raise |
| 4–6 | `agents/translator.py:444, 632, 867` | re-raise |
| 7–9 | `api/plugins.py:175, 202, 333` | `log.exception(...)`; schluckt (BG-Task) |
| 10–11 | `api/research.py:123, 149` | `log.exception(...)`; schluckt |
| 12 | `api/strategies.py:368` | TOCTOU-Guard: `refresh` + nur wenn nicht schon FAILED |
| 13 | `orchestrator/auto_runner.py:585` | `log.exception(...)`; schluckt; Feldreihenfolge error↔ended_at vertauscht |
| 14 | `orchestrator/paper_flow.py:173` | commit in try/except gewrappt („also failed to persist"); re-raise |
| 15–16 + 5 Aufrufe | `orchestrator/plugin_flow.py:216, 224, 261, 270, 290` | via `_finish_agent_run(..., error=describe_api_error(exc))` |

**Nicht Ziel** (kein `exc`, andere Semantik — unangetastet lassen):
- Cancel-Pfade: `researcher.py:222` (CancelledError → „Cancelled by user",
  commit suppressed), `api/research.py:285` (Cancel-Endpoint).
- `orchestrator/run_janitor.py:56, 102` (Batch-Sweeps mit `ORPHAN_ERROR`/`STALE_ERROR`).
- `_finish_agent_run` für **Erfolgspfade** (DONE + Artefakte) bleibt bestehen.

## Design

### Neues Modul `src/fwbg_agents/persistence/agent_runs.py`

Begründung Ort: wird von `agents/`, `api/` und `orchestrator/` gebraucht;
`persistence` ist die einzige Schicht, die alle drei bereits importieren, ohne
Zyklusrisiko (`tools/api_errors` importiert nur `tools/fwbg_client` +
Third-Party). `agents/ → orchestrator/` wäre zyklisch.

```python
import contextlib
import logging
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from fwbg_agents.persistence.models import AgentRun, AgentRunStatus
from fwbg_agents.tools.api_errors import describe_api_error

log = logging.getLogger(__name__)


async def fail_agent_run(
    session: AsyncSession,
    ar: AgentRun,
    exc: BaseException,
    *,
    transient: bool = False,
) -> str:
    """Mark `ar` FAILED with the classified error message and commit.

    Returns the stored message (for event emission at the call site).
    Commit failures are logged, never raised — the error path must not
    mask the original exception.
    """
    msg = describe_api_error(exc)
    if transient:
        msg = f"transient: {msg}"
    ar.status = AgentRunStatus.FAILED.value
    ar.ended_at = datetime.now(UTC)
    ar.error = msg
    try:
        await session.commit()
    except Exception:
        log.exception("failed to persist FAILED status (agent_run %s)", ar.id)
    return msg
```

### Bewusst schlank gehalten (keine Parameter-Explosion)

- **`log.exception`-Aufrufe bleiben an den Call-Sites** — sie tragen
  kontextspezifische Meldungen („author background task failed …").
- **Event-Emission bleibt beim Researcher** (einzige Stelle):
  `msg = await fail_agent_run(...)` → `event_bus.emit({..., "error": msg})`.
  Emission für alle Agenten generalisieren = Verhaltensänderung fürs
  Dashboard → separates Follow-up, siehe unten.
- **`transient` bleibt explizite Entscheidung des Aufrufers** (nur Runner:
  `transient=isinstance(exc, httpx.TransportError)`). Der Prefix ist
  semantisch an die Retry-Cap-Query `LIKE 'transient: %'` in
  `auto_runner.py:166` gekoppelt; ihn automatisch an allen 16 Stellen zu
  setzen würde deren Verhalten ändern.
- **TOCTOU-Guard bleibt in `api/strategies.py`** (refresh + if), innen dann
  der Helper-Aufruf.
- **Cancel-Pfade bekommen keinen eigenen Helper** — 2 Stellen mit bewusst
  unterschiedlicher Semantik, kein Drift-Risiko durch Klassifikation.

### Bewusste Verhaltensänderung (im PR dokumentieren)

Commit-Fehler im Error-Pfad werden überall geloggt statt propagiert (heute
nur in `paper_flow.py` so). An den Agent-Sites konnte bisher ein
Commit-Fehler die ursprüngliche Exception ersetzen — das ist strikt
schlechter; die Generalisierung des paper_flow-Verhaltens ist gewollt.

## Schritte

1. **Helper + Unit-Tests** (`tests/persistence/test_agent_runs.py`):
   setzt status/ended_at/error, gibt Meldung zurück, `transient=True`-Prefix,
   Commit-Fehler wird geschluckt + geloggt (Session-Mock mit failing commit).
   → verifizieren: neue Tests grün.
2. **Migration der 16 Stellen** (mechanisch, Site-Eigenheiten laut Tabelle
   erhalten: re-raise/schlucken, log.exception, Guard, Event mit
   Rückgabewert). In `plugin_flow.py` die 5 FAILED-Aufrufe von
   `_finish_agent_run` auf `fail_agent_run` umstellen; `_finish_agent_run`
   behält die Erfolgspfade, der `error`-Parameter dort fliegt raus, wenn er
   danach ungenutzt ist. Waisen aufräumen: ungenutzte
   `describe_api_error`/`datetime`-Importe je Datei.
   → verifizieren:
   ```
   grep -rn "describe_api_error" src/ | grep -v "tools/api_errors\|persistence/agent_runs"   # leer
   grep -rn "AgentRunStatus.FAILED.value" src/                                               # nur janitor + 2 Cancel-Pfade + Helper
   ```
3. **Volle Verifikation**: `uv run ruff check src/ tests/` + volle Suite
   (Erwartung: bestehende Message-Assertions unverändert grün, da die
   erzeugten Meldungen identisch bleiben) + `graphify update .`.
   → verifizieren: Suite grün, CI auf dem PR grün (echter Runner, nicht nur
   lokal).

Geschätzter Umfang: ~1 neue Datei (~40 Zeilen), ~11 Dateien je −3/+1 Zeilen
netto, ~4 Unit-Tests.

## Explizit out of scope (separate Entscheidungen)

- `agent_run_failed`-Events für **alle** Agenten emittieren (Dashboard-Nutzen,
  aber Verhaltens-/UI-Änderung).
- Transient-Erkennung in den Helper ziehen (`is_transient(exc)`) und die
  `LIKE 'transient: %'`-Kopplung in auto_runner durch ein echtes Feld
  (`AgentRun.transient: bool`) ersetzen — sauberer, braucht aber Migration.
- Die Cancel-Pfade und Janitor-Sweeps vereinheitlichen.
