# Plan 007: `fwbg-agent`-Namespace — agent-generierte Plugins taggen, suchen, filtern

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md`.
>
> **Drift check (run first)**:
> `git -C ~/Projekte/fwbg-agents diff --stat c3d93f8..HEAD -- src/fwbg_agents/tools/fwbg_client.py src/fwbg_agents/orchestrator/plugin_flow.py src/fwbg_agents/orchestrator/plugin_catalog.py`
> `git -C ~/Projekte/fwbg log --oneline -5 -- src/fwbg/api/plugins.py src/fwbg/pipeline/registry.py`
> `git -C ~/Projekte/fwbg-dashboard log --oneline -3 -- pages/plugins/index.vue`
> Bei Abweichungen von den „Current state"-Auszügen: STOP.
> **Achtung**: Das fwbg-Working-Copy steht aktuell auf Branch
> `feat/broker-mandatory-stop-loss`. fwbg-Schritte auf einem frischen Branch
> von `main` beginnen, nicht auf dem Broker-Branch aufsetzen.

## Status

- **Priority**: P2 (Feature; behebt nebenbei einen echten Defekt — siehe „Kernbefund")
- **Effort**: M (fwbg M + fwbg-agents S + Dashboard S)
- **Risk**: MED — `POST /api/plugins` ist ein Code-Upload-Endpoint; Validierung/Bind beachten (siehe Schritt 2 + STOP)
- **Depends on**: none (unabhängig von Plan 006)
- **Repos**: `fwbg` (Schritte 1–4), `fwbg-agents` (Schritt 5), `fwbg-dashboard` (Schritt 6)
- **Planned at**: fwbg-agents `c3d93f8`, fwbg main `~986c658^`, 2026-07-10

## Ziel (User-Anforderung)

Vom Plugin-Agenten erstellte Plugins sollen auf der Plugins-Seite des
Dashboards als eigene Quelle **`fwbg-agent`** erscheinen (analog zu
`fwbg-core`, `fwbg-premium`, `custom`) und über Suche **und** einen
Quellen-Filter schnell auffindbar sein.

## Kernbefund (verifiziert am 2026-07-10)

**Der Registrierungspfad ist halb gebaut und läuft heute ins Leere:**

- fwbg-agents ruft nach erfolgreicher Verifikation
  `_register_verified_plugin_in_fwbg()` auf
  (`orchestrator/plugin_flow.py:393` ff.) →
  `FwbgClient.register_plugin()` (`tools/fwbg_client.py:245`) →
  `POST /api/plugins` mit `{slug, python_code, kind, description, spec_md,
  tests_code, version, overwrite}`. Docstring verspricht Registrierung als
  `agent-authored:<slug>` in fwbgs User-Plugins-Verzeichnis.
- **fwbg hat diesen Endpoint nicht** — weder auf `main` noch auf dem
  aktuellen Branch existiert ein `POST /api/plugins` oder der String
  `agent-authored` (`git grep "agent-authored" main -- src/` → leer; einziger
  POST in `api/plugins.py` ist `/{fqn}/tests/run`, main L196).
- Folge: Die Registrierung schlägt bei jedem verifizierten Plugin still fehl
  (best-effort `log.warning`, plugin_flow.py:426 ff.) — agent-generierte
  Plugins erscheinen **nie** im fwbg-Registry/Dashboard.

**fwbg-Registry-Mechanik (Namespace ist frei, kein Enum):**

- `PluginRegistry.register(cls, namespace)` baut `fqn = f"{namespace}:{name}"`
  (`src/fwbg/pipeline/registry.py:81`); Namespace kommt aus dem
  Paket-`manifest.json` `"name"` (`discover_package`, L255).
- Discovery-Roots (`auto_discover`, L463–495): (1) `src/fwbg/plugins/`
  (→ `fwbg-core`, `custom`), (2) Entry-Points `fwbg.plugin_packages`
  (→ `fwbg-premium`), (3) **User-Dir `~/.fwbg/plugins/`** (L489–493) —
  hier gehören runtime-registrierte Agent-Plugins hin (nichts im
  Source-Tree; deckt sich mit der Design-Doc-Regel „PluginAuthor writes …
  never directly into fwbg").
- `registry.list_plugins(phase, namespace)` unterstützt Namespace-Filter
  bereits (L145–146); **die HTTP-API exponiert ihn nicht**
  (`src/fwbg/api/plugins.py:119–132`, nur `phase`).
- Präzedenzfall für Runtime-Registrierung + Cache-Invalidierung:
  `src/fwbg/api/custom_signals.py` (`_invalidate_registry`, L31–35).
- Kategorien sind fix (registry.py L259); `kind` aus fwbg-agents muss auf
  eine davon gemappt werden (`indicators`, `exit_strategies`, …).
- Hardcodierte Namespace-Auflösung: `packages/fwbg-mcp/src/fwbg_mcp/server.py:290`
  probiert nur `["fwbg-core:{name}", "fwbg-premium:{name}", name]`.

**Dashboard**: `pages/plugins/index.vue` filtert client-seitig nur nach
`phase` (L21) + Textsuche (L26); `plugin.namespace` wird bereits angezeigt
(L94). Datenquelle ist die fwbg-API (Port 8420), nicht fwbg-agents.

## Design-Entscheidungen

1. **Namespace-Name: `fwbg-agent`** (User-Wording). Der Client-Docstring in
   fwbg-agents sagt bisher `agent-authored` — da der Server nie existiert
   hat, ist die Umbenennung folgenlos; beide Seiten werden auf `fwbg-agent`
   ausgerichtet.
2. **Ablageort: User-Plugins-Dir** (`~/.fwbg/plugins/fwbg-agent/…`), nicht
   der fwbg-Source-Tree. Hält die gesperrte Regel „nie direkt ins Core-Repo"
   ein; der (unge baute) PromoteAgent M8 bleibt der Weg in den Source-Tree
   und ist von diesem Plan unberührt.
3. **Kein neues Registry-Konzept**: `fwbg-agent` ist ein gewöhnliches
   Plugin-Paket mit `manifest.json {"name": "fwbg-agent"}`, das der neue
   Endpoint bei Bedarf anlegt. Registry-Code bleibt unangetastet.
4. **Server-seitiger `namespace`-Filter** in `GET /api/plugins` (Registry
   kann es schon), Suche bleibt client-seitig im Dashboard.

---

## Schritte

### Schritt 1 (fwbg) — Agent-Paket-Layout im User-Dir

Helper in fwbg (z. B. `src/fwbg/api/agent_plugins.py` oder neben
`custom_signals.py`):

- `agent_package_dir() = get_user_plugins_dir() / "fwbg-agent"`; legt bei
  Erstbenutzung `manifest.json` an:
  `{"name": "fwbg-agent", "version": "1.0.0", "description": "Agent-authored plugins (verified by fwbg-agents)"}`.
- Pro Plugin: `<category>/<slug>/manifest.json` (`name`, `version`,
  `description`) + `__init__.py` (= `python_code`), optional `spec.md`
  (`spec_md`) und `test_<slug>.py` (`tests_code`) — Layout wie von
  `_discover_plugins_in_category` (registry.py L268–358) verlangt; `spec.md`
  wird dann vom bestehenden `GET /{fqn}/spec` (plugins.py L227) gefunden.

**Verifizieren**: Unit-Test, der ein Mini-Plugin in ein tmp-User-Dir schreibt
und via `discover_package` als `fwbg-agent:<slug>` registriert bekommt.

### Schritt 2 (fwbg) — `POST /api/plugins` implementieren

Contract exakt gegen den bestehenden Client bauen
(`fwbg-agents/tools/fwbg_client.py:245–279` ist die Referenz):

- Request: `{slug, python_code, kind, description="", spec_md="",
  tests_code="", version="1.0.0", overwrite=false}`.
- `kind` → Kategorie mappen (mindestens `indicator→indicators`,
  `exit_strategy→exit_strategies`; tatsächliche kind-Werte aus
  fwbg-agents `PluginState`/`plugin.kind` ableiten — beim Implementieren
  gegen `fwbg-agents/persistence/models.py` prüfen); unbekannter kind → 422.
- Validierung vor dem Schreiben (**Pflicht, Code-Upload!**):
  `slug` gegen dasselbe Pattern wie fwbg-agents' `SLUG_PATTERN`
  (kein `/`, kein `..`); `python_code` per `ast.parse` + demselben
  Import-/Call-Allowlist-Ansatz, den fwbg-agents' Contract-Check nutzt
  (`plugin_implementer.py::_check_imports_and_calls` als Vorlage); genau
  **eine** `BasePlugin`-Subklasse; Größenlimit (z. B. 256 KB). 422 bei
  Verstoß.
- `overwrite=false` + Slug existiert → 409; sonst Dateien schreiben
  (Schritt-1-Helper), Registry-Cache invalidieren (Muster
  `custom_signals._invalidate_registry`), Antwort
  `{"fqn": "fwbg-agent:<slug>", "category": <cat>, "slug": <slug>}`.
- Der Import beim Discovery **führt den Code aus** (dynamic import) — das
  passiert erst nach bestandener AST-Validierung, und der Endpoint ist wie
  die übrige fwbg-API localhost-gebunden. Trotzdem im Code kommentieren und
  im PR-Text als Sicherheitsentscheidung ausweisen (Parallele zu SEC-03 in
  `fwbg-agents/plans/README.md`).

**Verifizieren**: API-Tests — Happy Path (register → `GET /api/plugins`
enthält `fwbg-agent:<slug>`), 409 ohne overwrite, 422 bei bösem Import
(`import os` o. ä.), 422 bei Slug `../evil`.

### Schritt 3 (fwbg) — `namespace`-Filter in `GET /api/plugins`

`list_plugins()` (`api/plugins.py:119`) um optionalen Query-Param
`namespace: str | None` erweitern → durchreichen an
`registry.list_plugins(phase, namespace)` (Unterstützung existiert,
registry.py L145). Unbekannter Namespace = leere Liste (kein 400 —
Namespaces sind frei).

**Verifizieren**: Test `GET /api/plugins?namespace=fwbg-agent` liefert nur
Agent-Plugins; `?namespace=fwbg-core` unverändert die Core-Liste.

### Schritt 4 (fwbg) — MCP-Auflösungsreihenfolge ergänzen

`packages/fwbg-mcp/src/fwbg_mcp/server.py:290`: `"fwbg-agent:{name}"` in die
Kandidatenliste aufnehmen (nach `fwbg-premium`, vor dem Raw-Fallback).

### Schritt 5 (fwbg-agents) — Client & Katalog auf `fwbg-agent` ausrichten

- `tools/fwbg_client.py:245–266`: Docstring/Kommentar von
  `agent-authored:<slug>` auf `fwbg-agent:<slug>` korrigieren; Log-Meldung
  in `plugin_flow.py:422–425` ebenso.
- `_register_verified_plugin_in_fwbg` (plugin_flow.py:393): Response-FQN
  loggen und als Event persistieren, sobald Plan 006 Schritt 1 gelandet ist
  (`plugin_registered_in_fwbg` mit fqn) — sonst nur Log.
- Prüfen, dass der Live-Katalog (`orchestrator/live_catalog.py`, holt
  Katalog per HTTP von fwbg) neue `fwbg-agent:`-Plugins automatisch
  aufnimmt, damit Researcher/Translator sie referenzieren können —
  erwartet: ja, da namespace-agnostisch über `GET /api/plugins`; per Test
  mit Fake-Katalog bestätigen. Falls irgendwo `fwbg-core:`/`fwbg-premium:`
  hart gefiltert wird (grep!), Stelle erweitern.
- **Bekannter offener Punkt** (nicht Teil dieses Plans, dokumentieren):
  Retry-Pfad, wenn die Registrierung fehlschlägt — heute best-effort ohne
  Wiederholung; Zustand `VERIFIED` in fwbg-agents ≠ „in fwbg sichtbar".
  Als Folge-Issue notieren (Janitor-Resync oder Registrierung beim
  `reiterate_with_plugin`-Precondition-Check nachholen).

**Verifizieren**: `uv run pytest tests/ -x` grün; Integrationstest mit
laufendem fwbg: Plugin-Evaluate bis VERIFIED treiben (oder
`_register_verified_plugin_in_fwbg` direkt mit Fixture-Plugin aufrufen) →
`curl localhost:8420/api/plugins?namespace=fwbg-agent` zeigt das Plugin.

### Schritt 6 (fwbg-dashboard) — Quellen-Filter + Suche + Badge

`pages/plugins/index.vue`:

- **Quellen-Filter**: zweites `USelect` neben dem Phase-Filter mit den
  dynamisch aus den geladenen Plugins abgeleiteten Namespaces
  (`[...new Set(plugins.map(p => p.namespace))]`, Option „Alle") —
  kein Hardcoden, dann erscheint `fwbg-agent` automatisch. Alternativ (wenn
  server-seitig gewünscht): Query-Param aus Schritt 3 nutzen; client-seitig
  reicht bei 65+N Plugins aber aus.
- **Suche**: Suchfeld (L26) zusätzlich gegen `namespace` und `fqn` matchen
  lassen, damit die Eingabe „fwbg-agent" alle Agent-Plugins findet.
- **Badge**: `fwbg-agent`-Namespace visuell abheben (eigene Badge-Farbe,
  z. B. violett, konsistent in Liste und Detailseite `pages/plugins/[id].vue`
  — dort prüfen, wie namespace dargestellt wird).
- Optional (S): auf der Plugin-Detailseite bei `fwbg-agent`-Plugins ein Link
  „Zur Agent-Historie" auf die fwbg-agents-Plugin-Ansicht
  (`/agents/plugins`), Matching über Slug — Provenienz (Spec, Verification
  Runs, Post-Mortem) liegt dort.

**Verifizieren (E2E, manuell)**: fwbg + Dashboard starten, per curl ein
Test-Plugin registrieren → Plugins-Seite: Karte mit Badge `fwbg-agent`,
Filter „fwbg-agent" zeigt nur dieses, Suche „fwbg-agent" findet es,
Detailseite öffnet.

## STOP conditions

- fwbg-`main` hat inzwischen doch einen `POST /api/plugins` oder eine
  andere Registrierungs-Route bekommen → Contract abgleichen statt parallel
  zu bauen.
- Die `kind`-Werte aus fwbg-agents lassen sich nicht sauber auf fwbgs
  Kategorienliste (registry.py L259) mappen → STOP, Mapping mit Maintainer
  klären (falsche Kategorie = falsche Pipeline-Phase).
- Das dynamische Importieren hochgeladenen Codes wird vom Maintainer als zu
  riskant eingestuft, solange die API keine Auth hat (SEC-03) → STOP;
  Alternative diskutieren (Registrierung nur als „pending" auf Disk,
  Aktivierung per CLI/manuell).
- `get_user_plugins_dir()` ist im Deployment nicht beschreibbar (Container!)
  → Pfad konfigurierbar machen statt hart `~/.fwbg`; als eigener
  Mini-Schritt, nicht improvisieren.

## Explizit out of scope

- PromoteAgent / PR in den fwbg-Source-Tree (M8, Design-Doc §405).
- Auth für die fwbg-API (SEC-03-Komplex, Deployment-Entscheidung).
- Automatischer Resync fehlgeschlagener Registrierungen (als Folge-Issue
  notiert, Schritt 5).
- Versionierung/Upgrade-Pfad bereits registrierter Agent-Plugins
  (`overwrite=True` überschreibt v1 — genügt für den aktuellen Stand).
