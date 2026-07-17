# Plan 020: PR-#133-Follow-ups — Timeframe-Rollout (Merge/Deploy, DAY_1-E2E, Broker-Mapping härten)

> **Executor-Hinweise**: WP1+WP2 sind operativ (Merge/Release/Verifikation im
> Live-Service), WP3+WP4 sind Code-Änderungen im **fwbg**-Repo
> (`/home/haex/Projekte/fwbg` — Achtung: Haupt-Checkout liegt auf einem
> Feature-Branch; `git fetch origin` und für WP3/WP4 von `origin/develop`
> branchen, **nachdem** PR #133 gemergt ist). Jede Verifikation ausführen; bei
> STOP-Bedingung anhalten und berichten. Danach Status-Zeile in
> `plans/README.md` aktualisieren.
>
> **Kontext**: PR #133 (`fix/timeframe-canonical-enum`, Head `1a24ad0`) macht
> das SDK-`Timeframe`-Enum zur Single Source of Truth (`.canonical`,
> `from_str()`) und ersetzt hartes Fold-Skipping durch adaptives Fold-Sizing
> (`plan_walk_forward`). Code-Review am 2026-07-17 abgeschlossen, 3 Punkte
> gefixt und gepusht; CI grün, `mergeable: CLEAN`. Deployte Stände bei
> Planerstellung: fwbg v2.17.2, fwbg-agents v0.26.2.

## Status

- **Priorität**: WP1/WP2 P1 (blockiert alle Index-/DAY_1-Strategien), WP3 P2, WP4 P3
- **Aufwand**: WP1 S (operativ), WP2 S (operativ), WP3 S–M, WP4 S
- **Risiko**: WP1/WP2 LOW; WP3 LOW–MED (Verhaltensänderung: fail-loud statt
  stillem Fallback am Broker-Datenpfad)
- **Hängt ab von**: PR #133 gemergt (WP2–WP4); WP2 zusätzlich vom Deploy (WP1)
- **Geplant am**: 2026-07-17 (fwbg PR-Head `1a24ad0`)

## Warum das wichtig ist

Der Timeframe-Fix ist gemergt erst dann wertlos-bis-bewiesen, wenn ein echter
DAY_1-Lauf im Live-Service durchläuft — genau das war das Fehlerbild (Runs
brachen nach ~3 s mit `insufficient_data_for_folds` ab, Strategie 184 /
Run 965 als Index-ToM-Retry wartet darauf). Zusätzlich hat das Review eine
Lücke sichtbar gemacht, die der PR bewusst nicht anfasst: Die Chart-/Broker-
Endpunkte akzeptieren durch `Timeframe.from_str()` jetzt **mehr** Timeframes
(H2, H4, W1, M30) als die Broker-Mappings kennen — die IG-/yfinance-Adapter
fallen bei unbekannten Timeframes **still** auf `"HOUR"` bzw. `"1h"` zurück
und liefern falsch gelabelte Daten. Das ist dieselbe Fehlerklasse (stiller
Fallback statt lautem Fehler), die der PR im Optimizer gerade beseitigt hat.

## Umsetzungsstand (2026-07-17)

- **WP1 (Merge)**: DONE — PR #133 gemergt in `develop` (Commit `557a636`,
  2026-07-17T11:48 UTC). Release/Deploy (`scripts/release.sh`) bewusst **nicht**
  ausgeführt — bleibt Maintainer-Aktion.
- **WP2 (DAY_1-E2E im Live-Service)**: OFFEN — operativ, braucht
  Dashboard-Zugriff.
- **WP3 + WP4**: DONE (fwbg Branch `fix/broker-timeframe-mapping`, unpushed) —
  IG-Resolution-Vokabular-Widerspruch geklärt (die Map ist korrekt, nur der
  Kommentar war falsch beschriftet und wurde korrigiert), `TIMEFRAME_TO_YF_INTERVAL`
  um `M30→"30m"`/`W1→"1wk"` ergänzt (`H2` bewusst ungemappt), stille Fallbacks
  in beiden IG-Adaptern durch `ValueError` ersetzt, `api/chart.py` reicht diese
  als HTTP 400 durch. Neue Tests `test_broker_ig_timeframe_mapping.py` (6) +
  `test_api_chart_timeframes.py` (6); volle Suite grün (2695 passed, 36 skipped),
  `ruff check` clean.

## Ist-Zustand (verifiziert am PR-Head, vor dem Merge)

- **PR #133**: `mergeable: MERGEABLE`, `mergeStateStatus: CLEAN`, CI
  „Run Tests" SUCCESS. Base `develop`. Volle Suite im Worktree: 2604 passed,
  36 skipped.
- **Release-Prozess**: `scripts/release.sh <patch|minor|major>` (fwbg);
  erwartet das Geschwister-Repo `../fwbg-dashboard`, Version aus Git-Tags.
- **Broker-Mappings** (Package `packages/fwbg-broker-ig/src/fwbg_broker_ig/`
  + Legacy-Kopie `src/fwbg/adapters/broker/ig/`):
  - `mappings.py:174` `TIMEFRAME_TO_RESOLUTION` — deckt alle 9 Enum-Werte ab,
    **aber**: der Kommentar darüber nennt als gültige IG-API-Werte
    `5MINUTE, 30MINUTE, 2HOUR, 4HOUR`, die Map liefert jedoch `MINUTE_5,
    MINUTE_30, HOUR_2, HOUR_4`. Widerspruch Map ↔ dokumentiertes
    IG-Vokabular — ungeklärt, ob Kommentar oder Map falsch ist.
  - `mappings.py:187` `TIMEFRAME_TO_YF_INTERVAL` — es fehlen `M30`, `H2`,
    `W1` (yfinance kennt `30m` und `1wk`; ein 2-Stunden-Intervall existiert
    dort nicht).
  - `adapter.py:239` `TIMEFRAME_TO_RESOLUTION.get(timeframe, "HOUR")` und
    `adapter.py:307` `TIMEFRAME_TO_YF_INTERVAL.get(timeframe, "1h")` —
    stille Fallbacks (Legacy-Adapter analog: `ig/adapter.py:300`).
- **Chart-API**: `src/fwbg/api/chart.py` (`/chart/ohlcv` POST,
  `/chart/indicator`) reicht seit PR #133 jeden per `from_str()` parsebaren
  Timeframe an den Adapter durch. Es gibt **keine** Tests für chart.py
  (API-Tests liegen flach unter `tests/test_api*.py`).

## Benötigte Kommandos

| Zweck | Kommando (in fwbg) | Erwartet |
|---|---|---|
| Merge | `gh pr merge 133 --merge` (als `haexhub`) | Merge in `develop` |
| Release | `./scripts/release.sh patch` | Tag + Build laut Skript |
| Tests | `uv run pytest -q` | alle grün (~31 min, parallelisiert) |
| Fokus-Tests | `uv run pytest -q tests/ -k "broker or chart"` | neue Tests grün |
| Lint | `uv run ruff check src/ packages/` | exit 0 |

## Scope

**In scope**:
- WP1: Merge PR #133, Release/Deploy nach bestehendem Prozess
- WP2: Verifikations-Re-Run der Index-/DAY_1-Strategien (fwbg-agents-Service, operativ — keine Code-Änderung)
- WP3: `packages/fwbg-broker-ig/.../mappings.py` + `adapter.py` und die Legacy-Kopie `src/fwbg/adapters/broker/ig/adapter.py`; Fehlerdurchleitung in `src/fwbg/api/chart.py` prüfen
- WP4: neue Testdatei für `api/chart.py`-Timeframe-Pfade

**Out of scope**:
- Dukascopy-`INTERVAL_*`- und sonstige On-the-wire-Vokabulare (bewusste API-Verträge, siehe PR-Beschreibung)
- fwbg-agents-Code (der Service konsumiert nur die fwbg-API)
- Fold-Sizing-Tuning (`plan_walk_forward`-Parameter) — erst nach Empirie aus WP2
- `RESAMPLE_RULE`-Export in `data/resample.py` (ungenutzt, aber dokumentierter Referenz-Export — liegen lassen)

## Git-Workflow

- WP3/WP4: neuer Branch `fix/broker-timeframe-mapping` von `origin/develop`
  (nach WP1-Merge). Conventional Commits; keine Claude/Anthropic-Referenzen.
  `uv.lock` nicht mitcommitten. Push/PR nur nach Anweisung.

## Schritte

### WP1: Merge + Release/Deploy (Maintainer-Aktion)

1. PR #133 mergen (`gh pr merge 133 --merge`, Account `haexhub`).
2. Release nach bestehendem Prozess (`scripts/release.sh` lesen und folgen;
   erwartet `../fwbg-dashboard` als Geschwister-Verzeichnis). Versionstyp:
   **minor** (neues Enum-API im SDK + Verhaltensänderung Fold-Sizing).
3. Deploy wie bei den letzten Releases (v2.17.2-Prozess).

**Verify**: deployter Service meldet die neue Version; `GET /chart/sources`
liefert Timeframes in kanonischer Form (`HOUR_1`, `DAY_1`).

### WP2: DAY_1-E2E-Bestätigung im Live-Service

1. Im fwbg-agents-Dashboard die Index-Strategie(n) re-runnen — konkret
   Strategie 184 (Index-ToM, letzter Versuch Run 965) und/oder eine
   DAY_1-Strategie über DAX + EURUSD.
2. Erfolgskriterien:
   - Run läuft deutlich länger als ~3 s und erzeugt Folds
     (Log: `Creating N walk-forward folds`), kein
     `insufficient_data_for_folds` für DAX (~3180 Bars) / EURUSD (~6746 Bars).
   - Bei knapper Historie erscheint ggf. `Adaptive folds: N instead of M` —
     das ist erwartetes Verhalten, kein Fehler.
   - `fold_results.json` / Ergebnis-Artefakte tragen plausible
     Fold-Dimensionen (oos_size ≪ 4000 bei DAY_1).
3. Ergebnis (auch ein negatives) in `plans/README.md`-Statuszeile festhalten.

**STOP**: Läuft weiterhin `insufficient_data_for_folds` auf → Datenlage
prüfen (`*_DAY_1.csv` vorhanden? Bars gezählt?) und berichten — **nicht**
`min_oos`/`train_floor` lockern.

### WP3: Broker-Timeframe-Mapping fail-loud + vollständig

1. **Faktenklärung zuerst** (STOP-relevant): Gegen die IG-API-Dokumentation
   prüfen, welches Resolution-Vokabular tatsächlich gilt — der
   Map-Kommentar (`5MINUTE, 2HOUR, …`) und die Map-Werte (`MINUTE_5,
   HOUR_2, …`) widersprechen sich. Falls die Map falsch ist, ist das ein
   eigenständiger, bisher stiller Produktionsbug (IG antwortet vermutlich
   mit einem Fehler oder Default). Befund dokumentieren; Korrektur nur mit
   Beleg.
2. `TIMEFRAME_TO_YF_INTERVAL` vervollständigen: `M30: "30m"`, `W1: "1wk"`.
   `H2` bewusst **nicht** mappen (yfinance kennt kein 2h-Intervall).
3. Stille Fallbacks entfernen (beide Adapter, Package + Legacy):
   `TIMEFRAME_TO_RESOLUTION[timeframe]` bzw. expliziter Check mit
   `ValueError`/aussagekräftiger Exception bei fehlendem Mapping statt
   `.get(…, "HOUR")` / `.get(…, "1h")`.
4. Fehlerdurchleitung: `api/chart.py` (`/chart/ohlcv`, `/chart/indicator`
   Broker-Pfad) muss aus der Adapter-Exception ein `HTTP 400` mit dem
   nicht unterstützten Timeframe machen (kein 500). Prüfen, wie die
   Endpunkte Adapter-Fehler heute behandeln, und minimal ergänzen.

**Verify**: neue Unit-Tests — unbekannter Timeframe am Adapter wirft; `H2` ist
**ausschließlich auf dem yfinance-Pfad** ungemappt und wirft dort (die
IG-Resolution-Map deckt `HOUR_2` ab → auf dem IG-Pfad kein Fehler). Der
Broker-Pfad-400-Test verwendet einen Stub-Adapter, der den fail-loud
`ValueError` wirft, und prüft, dass `/chart/ohlcv` daraus **400** macht (nicht
500, nicht stillschweigend HOUR-Daten) — er testet die `ValueError`→400-
Durchreichung in `chart.py`, nicht das reale IG-Mapping.

### WP4: Chart-API-Timeframe-Tests

Neue Testdatei `tests/test_api_chart_timeframes.py` (Muster der bestehenden
`tests/test_api*.py` übernehmen; CSV-Quelle über tmp-Verzeichnis mit
kanonisch benannten Fixture-Dateien registrieren):

- `GET /chart/sources`: Dateien mit Legacy- (`EURUSD_HOUR.csv`) und
  kanonischem Namen (`DAX_DAY_1.csv`) → Timeframes erscheinen kanonisch.
- OHLCV-Endpoint: Anfrage mit Legacy-Schreibweise (`"HOUR"`) auf kanonisch
  benannte Datei → Daten kommen (Pfad über `_best_native_file`-Kanonisierung).
- Indikator-MTF: Chart-TF `"HOUR"` + `indicator_timeframe "HOUR_1"` →
  Single-TF-Verhalten, **kein** 400 (Regression auf Review-Fix `1a24ad0`);
  `indicator_timeframe` unterhalb des Chart-TF → 400; unbekannter TF → 400.

**Verify**: `uv run pytest -q tests/test_api_chart_timeframes.py` grün;
danach `uv run ruff check src/ packages/` und volle Suite.

## Done-Kriterien

- [x] PR #133 gemergt (WP1) — ⏳ Release getaggt + Service deployt bleibt Maintainer-Aktion (offen)
- [ ] Mindestens ein DAY_1-Index-Run (DAX oder EURUSD) im Live-Service mit erstellten Folds, kein `insufficient_data_for_folds` (WP2, operativ, offen)
- [x] Kein `.get(timeframe, "HOUR")`/`.get(timeframe, "1h")` mehr in beiden IG-Adaptern; unbekannte Timeframes → Exception → HTTP 400 (WP3)
- [x] IG-Resolution-Vokabular-Widerspruch geklärt und dokumentiert (WP3 Schritt 1)
- [x] Chart-Timeframe-Tests vorhanden und grün (WP4)
- [x] `uv run pytest -q` + `ruff check` exit 0 in fwbg (2695 passed, 36 skipped)
- [x] Statuszeile in `plans/README.md` aktualisiert

## STOP-Bedingungen

- IG-Resolution-Frage (WP3 Schritt 1) ohne verlässliche IG-Doku/Empirie nicht
  entscheidbar → Befund berichten, Map nicht auf Verdacht ändern.
- WP2 zeigt weiterhin `insufficient_data_for_folds` → Datenlage berichten,
  keine Fold-Schwellen lockern.
- `release.sh` schlägt fehl, weil `../fwbg-dashboard` fehlt/abweicht →
  berichten statt Skript anpassen.
- Die beiden IG-Adapter (Package vs. Legacy in-tree) sind inzwischen
  divergiert (mehr als die bekannten Zeilenoffsets) → berichten; nicht
  blind beide patchen.

## Wartungsnotizen

- Bewusst offen gelassen: `data/config.py` wirft seit PR #133 beim
  Modul-Import `ValueError` bei unbekanntem `TIMEFRAME`-Env (fail loud, auch
  für den API-Server). Gewollt; falls das im Betrieb stört, ist der richtige
  Fix eine Validierung beim Service-Start mit klarer Meldung, nicht die
  Rückkehr zum stillen Fallback.
- Nach WP2-Empirie prüfen, ob die `TIMEFRAME_CONFIG`-Zielwerte für `DAY_1`
  (`window_size=2000, oos_size=500`) zu den realen Index-Historien passen —
  Tuning erst mit Daten.
