# Plan 008: Agent-Run-Timeline — Folgeaufgaben aus 006/007

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md`.
>
> **Drift check (run first)**:
> `git -C ~/Projekte/fwbg-agents log --oneline -8 -- src/fwbg_agents/orchestrator/plugin_flow.py src/fwbg_agents/agents/plugin_evaluator.py src/fwbg_agents/orchestrator/run_janitor.py src/fwbg_agents/orchestrator/auto_runner.py src/fwbg_agents/config.py`
> Compare the "Kontext"-Zeilennummern unten gegen den Live-Code. Bei
> Abweichung: als STOP-Bedingung behandeln und Zeilenanker neu suchen, bevor
> du editierst.

## Status

- **Ausgeführt**: 2026-07-11, Session auf Branch `feat/006-agent-run-detail` @ `a22aea8`
- **DONE**: Schritte 1 (agent_run_id-Verkabelung), 2 (plugin_registered_in_fwbg-Event), 3 (Szenario-Events), 4 (prune_run_dirs), 6 (resync_verified_plugins). Verifiziert: 572 pytest grün, 1 skipped.
- **DEFERRED**: Schritt 5 (parent_run_id-Drill-down). Bewusst zurückgestellt (P3/optional,
  „nur wenn Zeit ist"). Begründung im Abschnitt „Schritt 5 — Abschluss-Notiz" unten.
- **OFFEN**: Manual-E2E (Plan 006 Schritt 10, braucht LLM-Proxy + Browser)
- **Priority**: P2 (Schritte 2 + 6 = Korrektheit/Sichtbarkeit) / P3 (Rest = Politur/Ops)
- **Effort**: M gesamt (lauter S-Schritte; Schritt 5 = M, cross-repo)
- **Risk**: LOW (rein additiv — Timeline-Events, Cleanup) außer Schritt 5 (DB-Migration → MED)
- **Depends on**: 006 + 007 (beide in `develop` gemergt). Alle Bausteine existieren:
  `run_events.emit_run_event`, `agents/instrumented.run_instrumented`,
  `persistence/agent_runs.{start,finish,fail}_agent_run`, die
  `/agents/runs/{id}`-Endpoints und die Dashboard-Detailseite.
- **Repos**: `fwbg-agents` (Schritte 1–4, 6) + `fwbg-dashboard` (Schritt 5 Frontend-Teil)
- **Planned at**: `fwbg-agents` `feat/006-agent-run-detail` @ `a22aea8` (= 006-Merge-Stand;
  `develop` liegt ggf. davor — Drift-Check zuerst). Neue Migration wäre `0009` (letzte: `0008_strategy_queue_position.py`).

## Ziel

Die mit 006/007 bewusst zurückgestellten Timeline-Lücken schließen und zwei
Robustheits-/Ops-Punkte nachziehen, die durch das gemergte `run_events`-Modul
jetzt entsperrt sind. Kein neues Feature — nur Vervollständigung des bereits
gebauten Agent-Run-Detail-Bilds.

## Kontext — was bereits steht (gemergt) und was fehlt

- **Registrierung in fwbg** passiert best-effort in
  `_register_verified_plugin_in_fwbg` (`plugin_flow.py:361`); der Erfolg wird
  nur **geloggt** (`log.info` bei `plugin_flow.py:391`), es gibt **kein**
  Timeline-Event. Fehlschläge (`except` bei `plugin_flow.py:395`) verschwinden
  komplett im Log — auf der Detailseite unsichtbar.
- **Evaluator-Szenarien**: die Schleife in `PluginEvaluator.run`
  (`plugin_evaluator.py:130`) zählt `scenarios_run`/`scenarios_passed`
  (`plugin_evaluator.py:135,142`), emittiert aber **keine** Events — im
  Dashboard sieht man beim Evaluator-Run nichts von der Szenario-Progression.
- **`evaluate_plugin(session, plugin_id)`** (`plugin_flow.py:331`) kennt heute
  **keine** `agent_run_id`; Aufrufer ist
  `auto_runner._author_and_reiterate` (`auto_runner.py:283`, Ruf bei
  `auto_runner.py:306`). Für Schritte 2+3 muss die `agent_run_id` des
  Evaluator-Runs hier durchgereicht werden (Voraussetzung = Schritt 1).
- **run_janitor** kehrt nur stehengebliebene **DB-Zeilen** (`sweep_stale_runs`,
  `run_janitor.py:77`; `sweep_loop`, `run_janitor.py:113`) — die
  `data/agent-runs/<id>/`-Verzeichnisse (events.jsonl + Transkripte) werden
  **nie** aufgeräumt → Disk wächst unbegrenzt (in 006 explizit als Follow-up notiert).
- **Kein `parent_run_id`**: Flow-Runs (`research_and_translate`,
  `research_flow.py:215`; `author_plugin_from_strategy`, `plugin_flow.py:156`)
  und ihre Kind-Agent-Runs sind unverknüpft — 006 Schritt 7 wurde übersprungen.
- **Registrierung ohne Retry**: ist fwbg beim VERIFIED-Übergang offline, ist
  das Plugin lokal VERIFIED, aber **nie** in fwbg registriert und dort
  unsichtbar — es gibt keinen Resync-Pfad.

---

## Schritte

### Schritt 1 — `agent_run_id` durch den Evaluator-Flow fädeln (Vorbereitung für 2+3)

Optionalen Parameter durchreichen (bei `None` = uninstrumentiert, damit
bestehende Unit-Tests ohne Run weiterlaufen):

- `evaluate_plugin(session, plugin_id, *, agent_run_id: int | None = None)` (`plugin_flow.py:331`)
- `PluginEvaluator.run(plugin, *, agent_run_id: int | None = None)` (`plugin_evaluator.py:52`)
- `_register_verified_plugin_in_fwbg(plugin, *, agent_run_id: int | None = None)` (`plugin_flow.py:361`)
- Quelle der id: der Evaluator-Run in `_author_and_reiterate` (`auto_runner.py:283–306`).
  **Zuerst prüfen**, ob dieser Flow den Evaluator bereits in eine
  `start_agent_run`/`finish_agent_run`-Hülle (agent_name `plugin_evaluator*`)
  wickelt. Falls **ja**: dessen `ar.id` durchreichen. Falls **nein**: die
  Hülle nach dem 006-Muster ergänzen (rein additiv, `start_agent_run(...,
  agent_name="plugin_evaluator")` → `evaluate_plugin(..., agent_run_id=ar.id)`
  → `finish_agent_run`).

**Chirurgisch**: nur Signaturen erweitern + id durchreichen, keine
Evaluations-Logik anfassen.

**Verifizieren**: `uv run pytest tests/agents/test_plugin_evaluator.py tests/orchestrator/ -x`
grün; `agent_run_id` default `None` → keine Testanpassung nötig.

### Schritt 2 — `plugin_registered_in_fwbg`-Event (schließt 007 sichtbar) — **P2**

In `_register_verified_plugin_in_fwbg` (`plugin_flow.py:384–399`):

- Erfolg (nach `client.register_plugin(...)`, neben `log.info` L391):
  `emit_run_event(agent_run_id, "plugin_registered_in_fwbg", fqn=f"agent-authored:{plugin.slug}", slug=plugin.slug)`.
- Fehlschlag (`except`-Zweig L395): zusätzlich
  `emit_run_event(agent_run_id, "plugin_registration_failed", slug=plugin.slug, error=str(exc))`
  — best-effort-Fehler auf die Timeline heben, nicht nur ins Log.
- Beide Emits nur wenn `agent_run_id is not None`. Best-effort-Semantik
  bleibt: weiterhin **nicht** raisen.

**Verifizieren**: Unit-Test mit Fake-`FwbgClient`. Erfolg → `events.jsonl`
enthält `plugin_registered_in_fwbg` mit `fqn == "agent-authored:<slug>"`;
Client wirft → `plugin_registration_failed` vorhanden **und** keine Exception
propagiert (Aufruf kehrt normal zurück).

### Schritt 3 — Szenario-Zähler-Events im Evaluator (006 Schritt 5.6 nachgezogen) — **P3**

In der Szenario-Schleife (`plugin_evaluator.py:130–142`), nur wenn
`agent_run_id`:

- pro Szenario: `scenario_passed` (`name`, `index`, `total`) bzw.
  `scenario_failed` (`name`, `invariant_violated` = erster Fehler aus
  `scenario_errors`).
- nach der Schleife: `evaluation_done` (`scenarios_run`, `scenarios_passed`, `status`).

Kein Transkript (deterministisch, kein LLM-Call). `total` =
`len(contract.test_scenarios)`.

**Verifizieren**: Evaluator-Test mit einem bestehenden **und** einem
fehlschlagenden Contract → `events.jsonl` enthält die passende
`scenario_*`-Folge + `evaluation_done` mit korrekten Zählern.

### Schritt 4 — Retention/Cleanup von `data/agent-runs/` im run_janitor — **P3**

- Neue Einstellung in `Settings` (`config.py:9`):
  `run_events_retention_days: int = 30` (Namenskonvention wie
  `run_stale_sweep_seconds`). `0` = deaktiviert.
- Neu `async def prune_run_dirs() -> int` in `run_janitor.py`: über jedes
  Verzeichnis unter `settings.data_dir / "agent-runs"` iterieren; die id aus
  dem Verzeichnisnamen parsen; den zugehörigen `AgentRun` laden. Löschen
  (`shutil.rmtree`) **nur** wenn der Run **terminal** (DONE/FAILED) ist **und**
  `ended_at` älter als `retention_days`. Anzahl loggen.
- Aufruf aus `sweep_loop` (`run_janitor.py:113`) nach `sweep_stale_runs`.

**Datensicherheit**: ein Verzeichnis, dessen id nicht parsebar ist **oder**
zu dem **kein** `AgentRun` existiert **oder** dessen Run **nicht terminal**
ist, wird **übersprungen**, nie gelöscht (Schutz vor Race mit einem Run, der
noch nicht committet hat).

**Verifizieren**: Test — je ein terminales Run-Verzeichnis mit altem
`ended_at` und ein laufendes → `prune_run_dirs` entfernt nur das alte
terminale; `retention_days=0` löscht nichts; unparsebares Verzeichnis bleibt.

### Schritt 5 — (Optional, größer, cross-repo) `parent_run_id` Flow-Drill-down — **P3**

= Plan 006 Schritt 7, jetzt entsperrt. Nur angehen, wenn Zeit ist.

- Migration `0009_agent_run_parent.py`: nullable
  `parent_run_id INTEGER REFERENCES agent_run(id)` + Index.
- `parent_run_id` setzen an den Flow-Kind-Start-Stellen:
  `research_and_translate` (`research_flow.py:215`) → researcher/translator;
  `author_plugin_from_strategy` (`plugin_flow.py:156`) →
  planner/implementer/evaluator. Kind-Agenten bekommen einen optionalen
  `parent_run_id`-Param.
- `GET /agents/runs/{id}` liefert zusätzlich `parent_run_id` und
  `children: [{id, agent_name, status}]`.
- Dashboard `pages/agents/runs/[id].vue`: Eltern-Link + Kinder-Liste
  (Navigation zwischen Flow- und Kind-Run). Deutsche Strings inline.

**Verifizieren**: `uv run alembic upgrade head` auf einer **Kopie** von
`data/state.db`; Flow-Test (research-brief) → beide Kind-Runs tragen die
Flow-Run-id als `parent`; Detail-Endpoint liefert `children`; Dashboard
`bunx nuxi typecheck` grün.

#### Schritt 5 — Abschluss-Notiz (2026-07-11, DEFERRED)

Nach dem Drift-Check bewusst zurückgestellt (Maintainer-Entscheidung). Befund
aus der Code-Erkundung, damit ein späterer Executor nicht neu erheben muss:

- **Parent-Runs existieren bereits** im API-Pfad: `research_flow`
  (`api/research.py:198`), `plugin_author_flow` (`api/plugins.py:246`),
  `reiterate` (`api/research.py:244`), `plugin_evaluator_flow`
  (`api/plugins.py:282`). Die Prämisse „Flows haben keinen Run" stimmt nur für
  die Flow-*Funktionen* selbst — der API-Layer wickelt sie bereits in einen Run.
- **Zwei nicht im Plan erfasste Haken**:
  1. Die Flow-Funktionen (`research_and_translate`, `author_plugin_from_strategy`)
     kennen die Parent-id nicht — `parent_run_id` müsste durch ihre Signaturen
     **und** in die selbst-Run-erzeugenden Agenten (`Researcher.run`,
     `Translator.run_fresh`) gefädelt werden.
  2. Der **autonome** Pfad `auto_runner._author_and_reiterate` (`:283`) hat
     **keinen** `plugin_author_flow`-Wrapper — planner/implementer sind dort
     elternlose Geschwister, der Evaluator ein separater Run. Vollständige
     Verknüpfung erforderte dort einen zusätzlichen Wrapper-Run (Scope über den
     Plan hinaus).
- **Warum deferred**: Umfang ~7–9 Dateien über 2 Repos + DB-Migration 0009
  (Risk MED), aber Kind-Runs sind heute schon über `strategy_id`/`plugin_id`
  navigierbar → inkrementeller Wert = Politur. Kein Blocker; jederzeit als
  eigener PR nachziehbar.

### Schritt 6 — fwbg-Registrierungs-Resync (VERIFIED-aber-unregistriert) — **P2**

Problem: `_register_verified_plugin_in_fwbg` ist best-effort ohne Retry — ist
fwbg beim VERIFIED-Übergang offline, fehlt das Plugin dauerhaft in fwbg.

- Neu `async def resync_verified_plugins()`: von fwbg
  `GET /api/plugins?namespace=agent-authored` die vorhandenen Slugs holen
  (Contract identisch zu 007 Schritt 3, gegen `tools/fwbg_client.py` prüfen);
  jedes lokal **VERIFIED** Plugin, dessen Slug fehlt, erneut über
  `_register_verified_plugin_in_fwbg` registrieren. Bounded, best-effort, geloggt.
- Aufhängen: beim Startup (in `main.lifespan`, neben `fail_orphaned_runs`)
  **oder** periodisch in `sweep_loop`. Hinter einer Einstellung
  `plugin_resync_enabled: bool = True`.

**Design-Entscheidung vor dem Bau** (im Plan-Abschluss dokumentieren):
Startup-only genügt für „Deploy während fwbg-Downtime", periodisch fängt auch
Laufzeit-Downtime — Startup als Default vorschlagen (einfacher, deckt den
Hauptfall ab).

**Umgesetzt (2026-07-11)**: Startup-only gewählt. `resync_verified_plugins()`
hängt in `main.lifespan` neben `fail_orphaned_runs`, hinter
`plugin_resync_enabled: bool = True` (config). Deckt den Hauptfall (Deploy
während fwbg-Downtime); Laufzeit-Downtime bleibt bewusst außen vor (einfacher,
kein periodischer Sweep-Overhead).

**Verifizieren**: Test — VERIFIED-Plugin fehlt in der (gefakten)
fwbg-Auflistung → Resync ruft `register`; ist es vorhanden → übersprungen;
fwbg nicht erreichbar → geloggt, kein Raise.

---

## Manuelle Verifikation (Plan 006 E2E — steht noch aus)

Der einzige 006-Punkt, der headless nicht lief (braucht LLM-Proxy via
`ANTHROPIC_BASE_URL` + Browser). Nach Schritten 2+3 zeigt der E2E zusätzlich
die Szenario-/Registrierungs-Events. Checkliste (aus 006 Schritt 10):

1. Beide Services hoch, Dashboard offen.
2. `/research/brief` auslösen → aktive Researcher-Card klicken → Timeline
   (Suchqueries + URL-Liste) erscheint **live**, ohne Reload.
3. Nach Abschluss neu laden → identische Timeline aus Persistenz + volles
   Transkript (System-Prompt, `search_web`-Calls, Hypothese).
4. Plugin-Author-Flow auslösen → Evaluator-Run zeigt `scenario_passed/failed`
   + `plugin_registered_in_fwbg` (Schritte 2+3).
5. Runner-Run öffnen → Link auf `/runs/:id` funktioniert.
6. Laufenden Run von der Detailseite abbrechen → Status kippt live auf failed.

## STOP conditions

- Der Evaluator-Flow besitzt heute **keine** AgentRun-Hülle und das Ergänzen
  würde mehr als `start_agent_run`/`finish_agent_run` erfordern (echte
  Lifecycle-State-Änderung) → STOP, melden, Umfang klären, bevor Events
  verdrahtet werden.
- Migration `0009` (Schritt 5) lässt sich auf einer **Kopie** von
  `data/state.db` nicht sauber anwenden → STOP, **nicht** gegen die echte DB
  laufen lassen.
- `prune_run_dirs` (Schritt 4) würde ein Verzeichnis löschen, dessen Run in
  der DB fehlt / nicht terminal ist → überspringen + loggen, **nie** blind
  löschen (Datenverlust-Guard).
- Der fwbg-Listing-Contract für den Resync (Schritt 6) weicht von der in 007
  verifizierten Form `GET /api/plugins?namespace=agent-authored` ab → STOP,
  Client-Contract neu prüfen, bevor Registrierungen in einer Schleife laufen.

## Reihenfolge & Abhängigkeiten

1. **Schritt 1 → 2 → 3** zusammen (ein PR): Schritt 1 ist die
   `agent_run_id`-Verkabelung für 2+3; schließt die sichtbaren 006/007-Lücken.
2. **Schritt 4** unabhängig (Backend-only, Ops-Hygiene).
3. **Schritt 6** baut auf der Funktion aus Schritt 2 auf (gleiche
   Registrier-Funktion) — danach.
4. **Schritt 5** zuletzt (größter, cross-repo, optional).
5. **Manueller E2E** am Ende — validiert zugleich Schritte 2/3 (und 5).

## Explizit out of scope (siehe `plans/README.md`)

- Token-weises Live-Streaming, Mid-Run-Steering, USD-Kosten
  (`models.py` `cost_usd` bleibt NULL) — 006-v2-Kandidaten.
- DEBT-03 Translator-Split (jetzt entsperrt), SEC-03/04, PERF-01/02/03,
  TEST-03, DOCS — eigener Round, in `plans/README.md` „next round" gelistet.
- Live-Trading-Flow, Speckit-Dedup-Gate, autonome paper-analyze/calibrate-
  Schleife, Preset-Crystallization — „Direction options", Maintainer-Entscheidung.
