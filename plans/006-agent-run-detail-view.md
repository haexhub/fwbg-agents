# Plan 006: Agent-Run-Detailansicht — klickbare Agents mit Einblick in die LLM-Session

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md`.
>
> **Drift check (run first)**:
> `git -C ~/Projekte/fwbg-agents diff --stat c3d93f8..HEAD -- src/fwbg_agents/agents/ src/fwbg_agents/api/ src/fwbg_agents/events.py src/fwbg_agents/persistence/`
> and in the dashboard repo:
> `git -C ~/Projekte/fwbg-dashboard log --oneline -5 -- components/agents/ composables/useAgentEvents.ts server/api/agents/`
> If in-scope files changed since this plan was written, compare the
> "Current state" excerpts against the live code before proceeding; on a
> mismatch, treat it as a STOP condition.

## Status

- **Priority**: P2 (Feature)
- **Effort**: L (Backend M + Frontend M)
- **Risk**: LOW–MED (rein additiv; kein Eingriff in Lifecycle-Gates oder Trading-Pfade)
- **Depends on**: none (Synergie mit DEBT-01/02 „AgentRun-envelope duplication" aus `plans/README.md` — Schritt 2 erledigt das nebenbei)
- **Repos**: `fwbg-agents` (Backend, Schritte 1–7) + `fwbg-dashboard` (Frontend, Schritte 8–10)
- **Planned at**: fwbg-agents commit `c3d93f8`, 2026-07-10

## Ziel (User-Anforderung)

In der Agents-Übersicht (`/agents` im Dashboard) sollen aktive **und**
historische Agent-Runs anklickbar sein und auf eine Detailseite pro Run
führen, die zeigt, was der Agent gerade macht bzw. gemacht hat:

- **LLM-Agenten** (researcher, plugin_planner, plugin_implementer, translator,
  analyst, paper_analyst): Einblick in die LLM-Session — System-/User-Prompt,
  Tool-Calls mit Argumenten und Ergebnissen, finale strukturierte Antwort;
  live während des Runs und nachträglich vollständig.
- **Researcher speziell**: welche Suchqueries er stellt, welche URLs/Quellen
  er einsammelt und welche Strategie-Hypothese er baut.
- **Runner** (kein LLM): Absprung zur bestehenden Backtest-Run-Übersicht
  (`/runs/:id` im Dashboard).
- **Eingreifen**: Cancel/Retry direkt von der Detailseite (Endpoints
  existieren bereits).

## Current state (verifiziert am 2026-07-10)

**Backend `fwbg-agents`:**

- `AgentRun` (`persistence/models.py:217`): flache Zeile (agent_name, status,
  strategy_id, plugin_id, input/output_artifact_path, error, Zeiten).
  `LlmCall` (`models.py:273`): **nur** Token/Latenz/Modell pro LLM-Call —
  kein Inhalt. Transkripte werden heute weggeworfen.
- Endpoints (`api/runs.py:298,314`): `GET /agents/runs`,
  `GET /agents/runs/{id}` (flach). Cancel/Retry: `api/research.py:266,293`.
- SSE: `GET /events/stream` (`api/events.py`) über in-memory Bus
  (`events.py`) — **flüchtig**, kein Replay. Ein später geöffneter Client
  sieht nichts Vergangenes.
- Nur der **Researcher** emittiert Events (`agents/researcher.py:131,169,191,236,255`):
  `agent_run_started/done/failed`, `research_search` (Query),
  `research_results` (URLs+Titel) — jeweils mit `agent_run_id`. Alle anderen
  Agenten emittieren nichts.
- Alle LLM-Agenten laufen über pydantic-ai 2.0 `Agent.run()`. Verifiziert im
  Projekt-venv: `Agent.run()` akzeptiert `event_stream_handler`, und
  `pydantic_ai.messages.ModelMessagesTypeAdapter` +
  `FunctionToolCallEvent`/`FunctionToolResultEvent`/`PartStartEvent`
  existieren. Damit sind Live-Streaming **und** Transkript-Serialisierung
  ohne Bibliothekswechsel möglich.
- AgentRun-Erzeugung ist dupliziert: `plugin_flow.py:98` (`_start_agent_run`)
  + Inline-Blöcke in `api/runs.py:283`, `api/research.py:193,246`,
  `api/strategies.py:414,512`, `api/plugins.py`, `orchestrator/auto_runner.py:649`,
  `agents/researcher.py:121`, `orchestrator/paper_flow.py:120`.
- Verschachtelung ohne Verknüpfung: `research_flow`-Run (api/research.py) und
  darunter eigene `researcher`-/`translator`-Runs (research_flow.py:231
  „their own AgentRun rows") — **kein** `parent_run_id`.
- Implementer läuft N Refinement-Runden mit je einem `LlmCallMeta`
  (`plugin_implementer.py:337ff`), Planner/Implementer kennen ihre
  `agent_run_id` heute **nicht** (Orchestrator persistiert außen).

**Frontend `fwbg-dashboard`** (Nuxt 4 + Nuxt UI 4, kein i18n, deutsche
Strings inline):

- Agent-Cards: `components/agents/ActiveRunsCard.vue` (Tabs Aktiv/Historie
  L232–235; Cards L258–304; Abbrechen L288 → `POST /api/agents/runs/{id}/cancel`;
  Wiederholen L376). Cards sind **keine** Links. `AGENT_LABELS` (L202–210)
  hat u. a. keinen Eintrag für `plugin_implementer`.
- SSE-Konsum existiert: `composables/useAgentEvents.ts` → Proxy
  `server/api/agents/events.get.ts` → `/events/stream`.
- **Der Proxy `server/api/agents/runs/[id]/events.get.ts` existiert bereits**
  und dokumentiert wörtlich: „Returns 404 until fwbg-agents implements the
  endpoint" — der Vertrag für Schritt 3 ist also schon abgestimmt.
- Detailseite für Backtest-Runs existiert (`pages/runs/[id].vue`, Route
  `/runs/:id`) — Ziel des Runner-Absprungs. Für Agent-Runs existiert
  **keine** Detailseite.

## Design-Entscheidungen

1. **Persistenz der Timeline als JSONL-Datei pro Run**
   (`data/agent-runs/<id>/events.jsonl`), nicht als DB-Tabelle. Begründung:
   Repo-Konvention „SQLite für Metadaten, Filesystem für Artefakte"; Events
   sind ein append-only Log, es gibt keine Query-Anforderung über Runs
   hinweg; keine Migration nötig. (Alternative DB-Tabelle nur, falls später
   Aggregation gebraucht wird.)
2. **Transkript = pydantic-ai Message-History**, serialisiert mit
   `ModelMessagesTypeAdapter.dump_json(result.all_messages())` nach
   `data/agent-runs/<id>/transcript_<NNN>.json` (NNN = LLM-Call-Runde, wegen
   Implementer-Loop). Kein eigenes Format erfinden.
3. **Live-Granularität: Tool-Calls + Nachrichtenwechsel, keine Text-Deltas.**
   Der `event_stream_handler` emittiert `llm_tool_call` / `llm_tool_result`
   (Payload auf je 2 KB gekürzt). Token-weises Streaming ist v2 — bewusst
   nicht in Scope (SSE-Volumen, Queue maxsize=200).
4. **„Eingreifen" = Cancel/Retry.** Mid-Run-Steering (Nachricht in eine
   laufende Session injizieren) ist mit dem One-Shot-`Agent.run()`-Muster
   nicht sinnvoll möglich — explizit out of scope, im Plan-Abschluss als
   Follow-up-Idee dokumentieren.
5. **Sicherheit**: Transkripte enthalten UNTRUSTED-Web-Content (bereits
   getaggt via `_untrusted()`, researcher.py:52) — die UI rendert
   ausschließlich als Text/Code, niemals als HTML/Markdown-HTML. Der
   Artefakt-Endpoint liefert nur Dateien, deren realer Pfad unter
   `settings.data_dir` liegt (Path-Traversal-Guard), nur Text, max. 512 KB.

---

## Schritte

### Schritt 1 — Run-Event-Modul (Backend)

Neues Modul `src/fwbg_agents/run_events.py`:

```python
def run_dir(agent_run_id: int) -> Path            # settings.data_dir / "agent-runs" / str(id)
def emit_run_event(agent_run_id: int, type: str, **payload) -> None
    # 1) dict mit ts (UTC iso) + seq (monoton pro Run, aus Dateizeilenzahl beim ersten
    #    Zugriff gecacht) bauen
    # 2) append als JSON-Zeile nach run_dir(id)/"events.jsonl"  (mkdir parents)
    # 3) event_bus.emit({**event, "agent_run_id": id})  — unverändertes SSE-Verhalten
def read_run_events(agent_run_id: int, limit: int = 500) -> list[dict]
```

Schreibfehler (voller Datenträger etc.) loggen, nie raisen — Events dürfen
keinen Agenten-Run abbrechen. Kein `asyncio`-Lock nötig (Single-Process,
Appends einzelner Zeilen), aber Kommentar dazu.

**Verifizieren**: neuer Test `tests/test_run_events.py` — emit → Datei
enthält Zeile mit `seq`/`ts`/`type`; `read_run_events` liefert sie zurück;
SSE-Bus-Subscriber (aus `events.subscribe()`) empfängt das Event.

### Schritt 2 — Einheitliche Lifecycle-Events + Envelope-Konsolidierung

`persistence/agent_runs.py` erweitern (Synergie mit DEBT-01/02):

- `async def start_agent_run(session, *, agent_name, strategy_id=None, plugin_id=None, input_artifact_path=None, status=RUNNING) -> AgentRun`
  — insert+commit+refresh und danach `emit_run_event(ar.id, "agent_run_started", agent_name=...)`.
- `async def finish_agent_run(session, ar, *, status, output_artifact_path=None, error=None, plugin_id=None)`
  — Update+commit und `agent_run_done`-Event.
- `fail_agent_run` (existiert, `agent_runs.py:19`): am Ende
  `emit_run_event(ar.id, "agent_run_failed", agent_name=ar.agent_name, error=msg)` ergänzen.

Alle Erzeugungs-/Abschluss-Stellen darauf umstellen (Liste in „Current
state"; `plugin_flow._start_agent_run/_finish_agent_run` werden zu dünnen
Aliassen oder entfallen). **Chirurgisch**: keine Statusmaschinen-Änderung,
nur Ersetzen der Insert/Update-Blöcke.

**Verifizieren**: `uv run pytest tests/ -x` grün; bestehende Tests, die
AgentRun-Zeilen prüfen (z. B. `tests/orchestrator/test_plugin_flow.py`),
unverändert grün. `grep -rn "AgentRun(" src/fwbg_agents/api src/fwbg_agents/orchestrator src/fwbg_agents/agents`
zeigt nur noch die Helper-Definition.

### Schritt 3 — `GET /agents/runs/{id}/events` (Backend)

In `api/runs.py`:

```python
@router.get("/agents/runs/{agent_run_id}/events")
async def get_agent_run_events(agent_run_id, session=...) -> list[dict]
```

404 wenn der Run nicht existiert; sonst `read_run_events(id)` (leere Liste
ist ok — ältere Runs haben keine Datei). Antwortform: **nacktes Array**, so
erwartet es der bereits existierende Dashboard-Proxy
(`server/api/agents/runs/[id]/events.get.ts`, Typ `Array<Record<string, unknown>>`).

**Verifizieren**: API-Test in `tests/api/` — Run anlegen, 2 Events emitten,
GET liefert beide in seq-Reihenfolge; unbekannte id → 404.

### Schritt 4 — Instrumentierter LLM-Aufruf (Transkript + Live-Events)

Neues Modul `src/fwbg_agents/agents/instrumented.py`:

```python
async def run_instrumented(agent: Agent, user_prompt: str, *,
                           agent_run_id: int, round_idx: int = 1) -> AgentRunResult
```

- ruft `agent.run(user_prompt, event_stream_handler=handler)` auf; der
  Handler mappt `FunctionToolCallEvent` → `llm_tool_call`
  (tool_name, args als JSON-Text, auf 2 KB gekürzt) und
  `FunctionToolResultEvent` → `llm_tool_result` (gekürzt) auf
  `emit_run_event(agent_run_id, ...)`. Andere Event-Typen ignorieren.
- nach Abschluss: `ModelMessagesTypeAdapter.dump_json(result.all_messages())`
  nach `run_dir(id)/f"transcript_{round_idx:03d}.json"` schreiben; dazu ein
  Event `llm_round_done` mit model/round/tokens.
- Fehlerpfad: Exception unverändert durchreichen; Transkript-Schreibfehler
  nur loggen.

**Wichtig**: exakte Event-Klassennamen/Felder gegen die installierte
pydantic-ai 2.0.0 verifizieren
(`uv run python -c "from pydantic_ai.messages import FunctionToolCallEvent; print(FunctionToolCallEvent.__annotations__)"`),
nicht aus dem Gedächtnis schreiben.

**Verifizieren**: Test mit `pydantic_ai.models.function.FunctionModel` (oder
`TestModel`) mit einem Tool: nach dem Lauf existiert `transcript_001.json`,
ist als JSON parsebar, enthält System-Prompt, Tool-Call und Tool-Result;
`events.jsonl` enthält `llm_tool_call` + `llm_tool_result` + `llm_round_done`.

### Schritt 5 — Instrumentierung in die Agenten einziehen

Reihenfolge nach Nutzwert; jeweils `agent.run(...)` durch
`run_instrumented(..., agent_run_id=...)` ersetzen und agentenspezifische
Events ergänzen:

1. **researcher.py**: `agent.run` (L212) ersetzen. Bestehende
   `research_search`/`research_results`-Emits auf `emit_run_event` umstellen
   (Persistenz gratis). Neu: `hypothesis_ready` (Familie, Asset-Klasse,
   Kurzbeschreibung) nach erfolgreicher Validierung — das ist die
   „was für eine Strategie baut er"-Anzeige.
2. **plugin_planner.py / plugin_flow.py**: `run_plan(..., agent_run_id=planner_ar.id)`
   durchreichen (neuer optionaler Param, damit Unit-Tests ohne Run weiter
   funktionieren: bei `None` unNinstrumentiert laufen lassen).
3. **plugin_implementer.py / plugin_flow.py**: dito
   (`run_implement(..., agent_run_id=impl_ar.id)`), `round_idx` = Runde des
   Refinement-Loops; zusätzlich pro Runde `implementer_round_failed` mit den
   Contract-Check-Fehlern (bereits als Strings vorhanden).
4. **translator.py, analyst.py, paper_analyst.py**: gleiches Muster (je 1
   LLM-Call).
5. **runner.py** (kein LLM): deterministische Events —
   `backtest_submitted` (**mit fwbg run_id**, sobald der `FwbgClient`-Call
   sie liefert), `backtest_progress` (falls Polling-Schleife existiert),
   `backtest_done` (Metriken-Kurzfassung). Das fwbg-run_id-Event ist der
   Anker für den Dashboard-Link `/runs/:id`.
6. **plugin_evaluator.py**: `scenario_passed/failed`-Events (Zähler), kein
   Transkript (falls kein LLM-Call — beim Implementieren verifizieren).

**Verifizieren**: bestehende Agent-Tests grün (`uv run pytest tests/agents/ tests/orchestrator/ -x`);
je Agent mindestens ein Test, der nach einem Fake-Lauf `events.jsonl` prüft.

### Schritt 6 — Detail-Endpoint anreichern + Artefakt-Inhalt (Backend)

- `GET /agents/runs/{id}` (`api/runs.py:314`) erweitern um:
  `llm_calls` (Liste aus `llm_call`-Zeilen: model, input/output_tokens,
  latency_ms, created_at), `total_input_tokens`/`total_output_tokens`,
  `transcripts` (Liste vorhandener Runden-Dateien mit Größe),
  `artifacts` (input/output: Pfad, exists, size). Flaches Zusatzobjekt,
  bestehende Felder unangetastet (Dashboard-Typ `AgentRun` bleibt gültig).
- Neu: `GET /agents/runs/{id}/transcript?round=NNN` — Inhalt einer
  Transkript-Datei (JSON), 404 wenn nicht vorhanden.
- Neu: `GET /agents/runs/{id}/artifact?kind=input|output` — Textinhalt des
  Artefakts. **Guard**: `Path(...).resolve()` muss unter
  `settings.data_dir.resolve()` liegen, sonst 403; max. 512 KB; nur
  Text-Dateien (Suffix-Allowlist: .json, .md, .py, .txt).

**Verifizieren**: API-Tests inkl. Path-Traversal-Versuch
(`input_artifact_path="/etc/passwd"` → 403).

### Schritt 7 — (Optional, empfohlen) `parent_run_id` für Flow-Drill-down

Migration `0009_agent_run_parent.py`: nullable
`parent_run_id INTEGER REFERENCES agent_run(id)` + Index. Setzen an den
Stellen, wo Flows Kind-Agenten starten (research_flow → researcher/translator;
plugin_author_flow → plugin_planner/plugin_implementer): Researcher/Translator
bekommen einen optionalen `parent_run_id`-Konstruktor-/Methodenparam.
`GET /agents/runs/{id}` liefert zusätzlich `children: [{id, agent_name, status}]`.
Kann bei Zeitdruck entfallen — die Detailseite zeigt dann Flow-Runs ohne
Kind-Navigation.

**Verifizieren**: `uv run alembic upgrade head` auf Kopie von
`data/state.db`; Flow-Test: research-brief → beide Kind-Runs tragen die
Flow-Run-id als parent.

### Schritt 8 — Dashboard: Proxies + Typen

In `fwbg-dashboard` (`server/api/agents/runs/[id]/`):
`transcript.get.ts`, `artifact.get.ts` nach dem Muster von `events.get.ts`
(`fwbgAgentsFetch`, Query-Params durchreichen). `types/agents.ts`: Typen
`AgentRunDetail`, `AgentRunEvent`, `LlmCallSummary` ergänzen.

### Schritt 9 — Dashboard: Detailseite `/agents/runs/:id`

Neue Datei `pages/agents/runs/[id].vue` + Komponenten unter
`components/agents/run/`:

- **Header**: Agent-Label, Status-Badge (`agentRunStatusColor`), Strategie-/
  Plugin-Links, Modell, Token-Summen, Dauer; Buttons „Abbrechen" (running)/
  „Wiederholen" (failed) — bestehende Proxies. Polling über
  `useAgentRuns().pollRun` + Refetch bei `agent_run_done/failed`-SSE-Event.
- **Timeline** (Kernstück): initial Backfill via
  `GET /api/agents/runs/{id}/events`, danach live via `useAgentEvents`,
  gefiltert auf `agent_run_id === route.params.id`, Dedupe über `seq`.
  Darstellung je Typ: Suchquery (Lupe + Query-Text), Quellen (Linkliste der
  URLs mit Titeln — **die Researcher-Crawl-Ansicht**), Tool-Call
  (Name + Args einklappbar), Implementer-Runde (rot bei failed mit
  Fehlertext), Backtest-Events mit **Link auf `/runs/{fwbg_run_id}`**.
- **LLM-Session-Tab**: Transkript-Runden laden
  (`GET .../transcript?round=`), Renderer für pydantic-ai-Messages:
  system/user eingeklappt, Tool-Calls/-Results als Code-Blöcke, finale
  Antwort pretty-printed. **Nur Textrendering, kein v-html** (untrusted
  Web-Content). Bei laufendem Run: Hinweis „Session läuft — Timeline zeigt
  Live-Aktivität" + Auto-Reload des Transkripts bei `llm_round_done`.
- **Artefakte-Tab**: input/output anzeigen (`GET .../artifact`), JSON pretty.
- **Runner-Sonderfall**: kein Transkript-Tab; stattdessen prominenter
  Button „Zur Backtest-Übersicht" (`/runs/:id` aus `backtest_submitted`-Event,
  Fallback `/runs`).

Deutsche Strings inline (Repo-Konvention, kein i18n).

### Schritt 10 — Dashboard: Cards verlinken

`components/agents/ActiveRunsCard.vue`: Cards in **beiden** Tabs in
`<NuxtLink :to="\`/agents/runs/${run.id}\`">` wrappen (bzw. Karte klickbar
machen); Abbrechen/Wiederholen-Buttons mit `@click.stop.prevent` schützen.
`AGENT_LABELS` vervollständigen (mindestens `plugin_implementer`,
`plugin_evaluator_flow`, `translator_reiterate_flow`, `promote_live` — gegen
die agent_name-Liste aus „Current state" abgleichen). Ebenso im
`EventFeed.vue`: Events mit `agent_run_id` auf die Detailseite verlinken.

**Verifizieren (E2E, manuell)**:
1. Beide Services starten, Dashboard öffnen, `/research/brief` auslösen.
2. Auf die aktive Research-Card klicken → Detailseite: Suchqueries und
   URL-Listen erscheinen **live**, ohne Reload.
3. Nach Abschluss Seite neu laden → identische Timeline aus Persistenz +
   vollständiges Transkript sichtbar (System-Prompt, search_web-Calls,
   Hypothese).
4. Historie-Tab: alten Run anklicken → Detailseite mit Events/Transkript
   (bzw. leerer Timeline bei Runs von vor diesem Feature — kein Fehler).
5. Runner-Run öffnen → Link zur Run-Übersicht funktioniert.
6. Laufenden Run von der Detailseite abbrechen → Status kippt live auf failed.

## STOP conditions

- `Agent.run(event_stream_handler=...)` feuert bei `output_type`-Runs keine
  Tool-Events oder die Event-Klassen haben andere Felder als in Schritt 4
  angenommen → stoppen, tatsächliche API dokumentieren, Plan anpassen
  (Fallback: `agent.iter()`).
- Bestehende Tests brechen durch die Envelope-Konsolidierung (Schritt 2) an
  mehr als trivialen Stellen (Monkeypatch-Ziele in Tests!) → stoppen und
  Umfang neu schneiden, bevor weitere Call-Sites umgestellt werden.
- Transkript-Dateien > 5 MB pro Runde in der Praxis (Planner bettet
  Beispiel-Plugin-Quelltexte ein) → stoppen, Trunkierungsstrategie
  entscheiden statt stillschweigend abschneiden.
- SSE-Events kommen im Dashboard nicht an, obwohl `curl /events/stream` sie
  zeigt (Nitro-Proxy-Buffering) → als eigenes Infra-Problem melden, nicht in
  diesem Plan fixen.

## Explizit out of scope

- Token-weises Live-Streaming des LLM-Texts (v2-Kandidat).
- Mid-Run-Steering / Nachricht in laufende Session injizieren.
- Retention/Cleanup von `data/agent-runs/` (Disk wächst — als Follow-up am
  `run_janitor` notieren).
- Kosten-USD-Anzeige (LlmCall.cost_usd bleibt NULL, siehe models.py:277).
