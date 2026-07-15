# Plan 010: Profitabilitäts-Hebel — Datasource-Bugs, Zuverlässigkeit, Anti-Overfitting, Analyst-Tool-Use, Researcher-Kreativität

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md`.
>
> **Drift check (run first)**:
> ```
> git -C ~/Projekte/fwbg-agents log --oneline -8 -- \
>   src/fwbg_agents/agents/researcher.py src/fwbg_agents/agents/analyst.py \
>   src/fwbg_agents/agents/translator.py src/fwbg_agents/orchestrator/research_flow.py \
>   src/fwbg_agents/orchestrator/promote_gate.py src/fwbg_agents/orchestrator/trade_diagnostics.py \
>   src/fwbg_agents/config.py
> git -C ~/Projekte/fwbg log --oneline -5 -- \
>   src/fwbg/api/chart.py src/fwbg/api/signal_composer.py src/fwbg/core/data_sources.py
> ```
> Zeilenanker unten gegen den Live-Code prüfen. Bei Abweichung: Anker neu
> suchen, bevor du editierst; bei struktureller Abweichung STOP.

## Status

- **Status**: TODO

## Kontext & Motivation

Analyse vom 2026-07-14 (Session mit Maintainer). Befund: Der Engpass des
Systems ist nicht die Ideen-Quelle, sondern (1) Zuverlässigkeit/Durchsatz des
Loops — 3 Strategien erzeugt, alle noch `proposed`, 9 von 15 Researcher-Läufen
gescheitert — und (2) fehlende Multiple-Testing-Kontrolle am Promote-Gate.
Danach: Analyst-Tool-Use über per-Trade-Daten (explizit vom Maintainer
gewünscht), Researcher-Fan-out mit Kritiker, Regime-Labels,
Cross-Strategy-Lernen, Crossover-/Daten-Scan-Kreativmodus.

**Maintainer-Entscheidung**: Es wird nur noch **Dukascopy** als Datenquelle
genutzt. Jedes Auftauchen von `forexsb` außerhalb von Test-Fixtures ist ein
Bug bzw. Altlast.

Ausführungsreihenfolge: WP1 → WP2 → WP4 → WP3 → WP5 → WP6.
(WP4 vor WP3, weil der Maintainer Analyst-Tool-Use explizit priorisiert hat.)
WP1 ist Voraussetzung für alles Weitere — ohne laufenden Loop sind die
übrigen WPs nicht end-to-end verifizierbar.

---

## WP1 (P1, Effort S–M): forexsb-Altlasten raus, Dukascopy als einzige Quelle, Loop-Zuverlässigkeit

### Diagnose (verifiziert 2026-07-14)

- Die Fehlerserie `datasource='default' is not in the known catalog
  (['forexsb'])` (agent_run ids 14/17/18/21, 2026-06-27) hatte zwei Ursachen:
  (a) fwbg meldete live `['forexsb']`, weil im fwbg-Workspace nur
  `data/forexsb/` als Quelle existiert (Datenquellen sind
  verzeichnisbasiert: `data/<name>/config.json`, siehe
  `fwbg/src/fwbg/core/data_sources.py` Docstring), und (b) das LLM emittierte
  `'default'` statt eines Katalognamens. Der Validator-Teil ist in
  fwbg-agents bereits gefixt (Live-Liste statt frozen default,
  `strategy_validator.py:302-311`); der Workspace-Teil und die
  fwbg-Hardcodings nicht.
- Hardcodierte `"forexsb"`-Defaults in fwbg:
  `src/fwbg/api/chart.py:162` (`source: str = Query("forexsb")`),
  `src/fwbg/api/chart.py:322`, `src/fwbg/api/signal_composer.py:178`.
- Researcher-Ausfälle "Exceeded maximum output retries (1)": Der
  Researcher-Agent wird ohne `retries`-Parameter konstruiert
  (`researcher.py:134-138`), der Analyst hat `retries={"output": 3}`
  (`analyst.py:510-515`).
- Proxy-502er (`claude-opus-4-7` api_error) fraßen beide Fanout-Versuche.
  `llm_max_retries` default ist 1 (`config.py:38`).

### Schritte

1. **[fwbg, ops+code] Dukascopy-CSV-Quelle anlegen, forexsb stilllegen.**
   - Prüfen, mit welchem Workspace der fwbg-Server läuft, den fwbg-agents
     anspricht (`FWBG_WORKSPACE`/`FWBG_DATA_DIR`, sonst Repo-`data/`).
     Stand 2026-07-14: Repo-`data/` enthält nur `forexsb/`.
   - Eine CSV-Quelle `dukascopy` anlegen (`data/dukascopy/config.json`,
     Format wie die bestehende forexsb-`config.json`; der
     Dukascopy-Downloader `src/fwbg/data/dukascopy.py` schreibt fertige
     `T,O,H,L,C,V`-CSVs direkt in ein CSV-Quellverzeichnis — kein ETL nötig).
   - `data/forexsb/` NICHT löschen — nach `data/_retired/forexsb/` oder
     außerhalb des Workspace verschieben (STOP-Condition 1 beachten).
   - → verifizieren: `curl -s localhost:<fwbg-port>/api/data/sources` listet
     `dukascopy` und NICHT `forexsb`.
2. **[fwbg] Hardcodierte forexsb-Defaults entfernen.**
   - `api/chart.py:162`, `api/chart.py:322`, `api/signal_composer.py:178`:
     Default auf `None`; bei `None` zur Laufzeit auflösen: genau eine
     konfigurierte Quelle → diese nehmen; keine oder mehrere → 422 mit
     Liste der konfigurierten Quellen. KEIN neuer frozen Default
     (`"dukascopy"` hardcoden wäre derselbe Bug in grün).
   - Docstring-Beispiel `forexsb/` in `core/data_sources.py:8` auf
     `dukascopy/` umschreiben (kosmetisch, gleicher Commit).
   - → verifizieren: `grep -rn forexsb src/fwbg --include='*.py' | grep -v test`
     liefert 0 Treffer; bestehende fwbg-Tests grün.
3. **[fwbg-agents] Deterministische Datasource-Normalisierung im Translator.**
   - An den drei Validierungsstellen (`translator.py:424`, `:541`, `:699`,
     jeweils `datasources=live.datasource_names() or None`): Wenn das
     LLM-Output-`datasource` NICHT in der Live-Liste ist und die Live-Liste
     **genau einen** Eintrag hat, den Wert vor der Validierung deterministisch
     auf diesen Eintrag setzen (mit `log.warning` + Run-Event), statt den
     ganzen Lauf scheitern zu lassen. Bei ≥2 konfigurierten Quellen weiterhin
     hart failen (dann ist die Wahl echt). Als gemeinsame Helper-Funktion,
     nicht 3× kopieren.
   - → verifizieren: neuer Test in `tests/agents/test_translator_fresh.py`
     (Muster: bestehender Test bei `:232` "frozen 'forexsb' default … must be
     rejected"): LLM sagt `'default'`, Katalog `['dukascopy']` → Strategie
     wird mit `datasource='dukascopy'` persistiert, Lauf DONE.
4. **[fwbg-agents] Researcher-Output-Retries + LLM-Retry-Budget.**
   - `researcher.py:134-138`: `retries={"output": 3}` ergänzen (Gleichstand
     mit Analyst).
   - `config.py:38` `llm_max_retries`: Default 1 → 2 (der Anthropic-SDK-Retry
     greift bei 5xx; zusammen mit `llm_timeout_seconds` bleibt der
     Worst-Case begrenzt — Kommentar im Field entsprechend anpassen).
   - → verifizieren: `uv run pytest tests/agents/test_researcher.py
     tests/tools/ -q` grün.
5. **[ops, kein Code] Loop-Volumen.** Nach 1–4: auto_runner mehrere Tage
   laufen lassen (≥20 Research-Briefs), Fehlerquote in `agent_run`
   beobachten. Ziel: <10 % failed (heute 60 %). Erst danach WP3/WP5/WP6
   bewerten — deren Nutzen hängt an akkumulierten Läufen (`lessons.md`,
   Family-Histories existieren heute noch gar nicht).

---

## WP2 (P1, Effort M): Trial-Zählung + Deflated Sharpe Ratio am Promote-Gate

### Warum

Das Promote-Gate (Holdout + 2×-Kosten-Stress, `orchestrator/promote_gate.py`,
Plan 009 WP4) prüft jede Strategie isoliert. Eine Agenten-Fabrik ist aber eine
Massiv-Suche über denselben Datensatz: Bei 200 Trials sind ~10 „Treffer" mit
p<0.05 purer Zufall. Je besser WP1 den Durchsatz macht, desto größer das
Problem. Gegenmittel: Sharpe um die Suchbreite deflationieren (Bailey &
López de Prado, „The Deflated Sharpe Ratio", 2014).

### Schritte

1. **Trial-Zählung** — neues Modul `src/fwbg_agents/orchestrator/trials.py`:
   `count_trials(session) -> TrialCounts` mit (a) global: Anzahl aller
   abgeschlossenen Backtest-Läufe über alle Strategien/Iterationen (Quelle:
   `agent_run` + Iterations-Verzeichnisse), (b) pro Familie
   (`strategy.strategy_family`). Grid-Search-Kombinationen innerhalb eines
   Laufs zählen als Trials mit, wenn die Zahl aus den Run-Artefakten
   (`grid_details/`) ablesbar ist; sonst konservativ Läufe zählen und das im
   Docstring festhalten.
2. **DSR-Berechnung** — `deflated_sharpe_ratio(sr, sr_variance_across_trials,
   n_trials, n_obs, skew, kurtosis) -> float` in `trials.py` (oder
   `orchestrator/metrics.py`, wo `median_metrics_across_assets` schon lebt).
   Formel: erwartetes Max-Sharpe unter N Trials via
   E[max] ≈ sqrt(V[SR]) · ((1−γ)·Φ⁻¹(1−1/N) + γ·Φ⁻¹(1−1/(N·e))), dann
   PSR gegen diese Benchmark statt gegen 0. Skew/Kurtosis aus der
   Trade-P&L-Serie (`test_trades_detail` über alle Folds,
   Loader-Muster: `trade_diagnostics.py:94-107`). Gegen das Zahlenbeispiel
   aus dem Paper testen (STOP-Condition 2).
3. **Gate-Integration** — in `promote_gate.py`: DSR als dritte Prüfung neben
   Holdout und Kosten-Stress; Schwelle konfigurierbar
   (`config.py`: `dsr_min: float = 0.95`), Ergebnis + `n_trials` in
   `promote_gate_results.json` (der Analyst sieht das bereits über den
   `{{ promote_gate }}`-Slot). Ein DSR-Fail blockt Promote wie ein
   Holdout-Fail.
4. → verifizieren: Unit-Tests für Formel (Paper-Beispiel) + Gate-Fail bei
   hoher Trial-Zahl mit grenzwertigem Sharpe; `uv run pytest -q` gesamt grün.

---

## WP4 (P1 laut Maintainer, Effort M): Analyst-Tool-Use über per-Trade-Daten

### Warum

Der Analyst bekommt heute eine vorgerenderte `trade_diagnostics.md`
(Plan 009 WP1). Der Maintainer will, dass er selbst nachbohren kann:
„Loser gruppiert nach Entry-Stunde", „P&L konditional auf Haltedauer-Quartil".
Per-Trade-Rohdaten liegen in
`grid_details/<symbol>/fold_results.json → walk_forward.fold_details[].test_trades_detail`
(mit Timestamps, MAE/MFE — NICHT in `trades.json`).

### Schritte

1. **Trade-Store bauen** — in `orchestrator/trade_diagnostics.py` einen
   Loader ergänzen (bestehendes Lade-Muster `:94-107` wiederverwenden):
   alle `test_trades_detail`-Zeilen aller Symbole/Folds einer Iteration in
   eine **In-Memory-SQLite**-DB laden, eine Tabelle `trades`
   (Spalten = Trade-Dict-Keys + `symbol` + `fold`). Kein Filesystem-Zugriff
   aus dem Tool heraus.
2. **Analyst-Tool `query_trades(sql)`** — in `analyst.py` den Agent (heute
   tool-los, `:510-515`) um ein `@agent.tool_plain` erweitern, Muster:
   Researcher-Tools (`researcher.py:144-189`). Guardrails hart im Code:
   nur ein einzelnes `SELECT`-Statement (Reject bei `;`, PRAGMA, ATTACH,
   INSERT/UPDATE/…), `LIMIT`-Cap 200 Zeilen erzwingen, Ergebnis als
   kompaktes JSON. Fehlermeldungen (z. B. unbekannte Spalte) als String
   zurückgeben, damit das Modell selbst korrigieren kann.
3. **Zweites Tool `describe_trades()`** — Spaltenliste + row count +
   min/max Timestamp pro Symbol, damit das Modell das Schema nicht raten
   muss.
4. **Prompt** — `prompts/analyst.md`: Abschnitt „Du hast Query-Zugriff auf
   die einzelnen Walk-Forward-Trades" mit 2–3 Beispiel-Queries; Anweisung,
   vor `change_exit`/`modify_plugins`-Empfehlungen die Diagnose per Query zu
   erhärten und die entscheidenden Query-Ergebnisse in `reasoning` zu
   zitieren. `trade_diagnostics.md` bleibt als Zusammenfassung im Prompt.
5. **Events** — pro Tool-Call ein `emit_run_event(ar.id, "analyst_query",
   sql=…)` (Muster: `research_search`-Event, `researcher.py:159`), damit die
   Queries im Dashboard-Run-Detail sichtbar sind.
6. → verifizieren: Unit-Tests mit pydantic-ai `FunctionModel`, die (a) einen
   Tool-Call ausführen und ein plausibles Ergebnis zurückbekommen,
   (b) SQL-Injection-/Write-Versuche (`DROP`, `;--`, `ATTACH`) abgelehnt
   sehen, (c) den 200-Zeilen-Cap bestätigen. `uv run pytest
   tests/agents/test_analyst.py -q` grün.

---

## WP3 (P2, Effort M): Researcher-Fan-out mit Kritiker + Diversitäts-Druck

### Warum

`researcher_fanout_n` (default 2, `config.py:52`) ist heute reines
Reliability-Fanout: sequenziell, first-valid-wins
(`research_flow.py:77-110`). Hypothesen sind billig, Backtests teuer — der
billigste Qualitätsfilter ist, mehrere valide Kandidaten zu erzeugen und den
besten auszuwählen, bevor Translator+Backtest Geld kosten.

### Schritte

1. **Kandidaten-Modus** — `research_flow._run_researcher_fanout` erweitern:
   neuer Config-Wert `researcher_candidates_n: int = 3` (1 = heutiges
   Verhalten, kein Kritiker). Statt first-valid-wins: bis zu N **valide**
   Hypothesen sammeln (Fehlversuche zählen weiter gegen `fanout_n`-Budget
   pro Kandidat).
2. **Critic-Agent** — neu `src/fwbg_agents/agents/critic.py` +
   `prompts/critic.md` (Struktur-Muster: Analyst). Input: die N Hypothesen
   (als JSON) + Lessons-Digest + Prior-Art-Zusammenfassungen. Output
   (structured): pro Kandidat `{score: 0..1, kill_risks: [str],
   verdict: pass|reject}` + `winner_index`. Prompt-Kern: adversarial —
   „Warum wird das scheitern? Kostenrealismus, Regime-Abhängigkeit,
   Overfitting-Gefahr, Nähe zu abgebrochenen Ideen." Der Critic wählt nur
   aus, er kann NICHT alle durchwinken lassen entfallen: bei
   `verdict: reject` für alle → Lauf schlägt fehl wie heute
   `ResearcherFanoutExhaustedError`.
3. **Persistenz** — Critic-Report als Sidecar
   (`data/strategies/<slug>/critic_report.json`) + `agent_run`-Zeile
   (`agent_name="critic"`) + Run-Event; Verlierer-Hypothesen als Artefakte
   unter `data/agent-runs/…` behalten (Rohmaterial für WP6-Crossover).
4. **Diversitäts-Druck** — neuer Prompt-Slot `{{ exploration_balance }}` in
   `prompts/researcher.md`: deterministisch gerenderte Verteilung der
   bisherigen Strategien nach `strategy_family × asset_class × timeframe`
   (Query auf `strategy`-Tabelle; Renderer neben `lessons_digest()` in
   `orchestrator/lessons.py` oder neues Modul) + Instruktion, unterexplorierte
   Zellen zu bevorzugen, solange die Hypothese mechanistisch begründet
   bleibt. Stand heute wären alle 3 Strategien in derselben Zelle
   (mean_reversion × FX × intraday) — genau der Attraktor, den das bricht.
5. → verifizieren: Tests analog `tests/orchestrator/test_research_flow.py`
   mit Stub-Researcher/Stub-Critic: Gewinner wird übersetzt, Verlierer
   persistiert, `researcher_candidates_n=1` verhält sich exakt wie heute.

---

## WP5 (P2, Effort M, Spike zuerst): Regime-Labels + Interventions-Digest (Cross-Strategy-Lernen)

### Schritte

1. **Spike Regime-Quelle (½ Tag, STOP-Condition 3)** — Optionen:
   - (A) fwbg reichert `test_trades_detail` serverseitig um
     `vol_regime`/`trend_regime` pro Trade an (ATR-Perzentil bzw.
     MA-Slope/ADX zum Entry-Zeitpunkt) — cross-repo, aber die Labels
     entstehen dort, wo die Candles schon sind.
   - (B) fwbg-agents holt Candles über die fwbg-API und labelt clientseitig.
   - Empfehlung: A (kein Daten-Duplikat, ein Berechnungsort). Entscheidung
     mit Maintainer festhalten, dann umsetzen.
2. **Diagnostik-Integration** — `trade_diagnostics.py`: neue Buckets
   „per Regime" (analog Stunde/Wochentag); Spalten stehen damit automatisch
   auch dem WP4-`query_trades`-Tool zur Verfügung.
3. **Interventions-Digest** — Gegenstück zu `lessons.md` für *erfolgreiche*
   Interventionen: neues Modul (Muster: `orchestrator/lessons.py`,
   `regenerate_lessons_digest`): über alle Familien aus der Lineage
   (`orchestrator/lineage.py`) je Empfehlungsart (`tune_params`,
   `change_exit`, `modify_plugins`, …) das Median-Sharpe-Delta
   parent→child aggregieren → `data/interventions.md`, längenbegrenzt als
   neuer Slot `{{ interventions_digest }}` in `prompts/analyst.md`
   („bei anderen mean_reversion-Linien brachte ein Session-Filter median
   +0.3 Sharpe").
4. → verifizieren: Unit-Tests für Digest-Renderer (leere DB → Platzhalter;
   2 Familien mit bekannten Deltas → korrekte Aggregation); Regime-Spalten
   tauchen in `describe_trades()` auf.

---

## WP6 (P3, Effort L, Spike zuerst): Kreativ-Ausbau — Crossover-Modus + Daten-Scan-Tool

Erst angehen, wenn WP1-Volumen echte abgeschlossene Linien produziert hat
(≥5 Abandons mit Post-Mortem ODER ≥10 abgeschlossene Familien) — vorher
fehlt das Rohmaterial.

### Schritte

1. **Crossover-Modus im Researcher** — neuer Input-Modus
   (`ResearcherInput.mode: Literal["fresh", "crossover"]`): Bei `crossover`
   bekommt der Prompt ein deterministisch gerendertes Komponenten-Inventar
   aus abgeschlossenen Linien (Entry-Logik/Exit/Filter je Strategie mit
   Evidenz aus Post-Mortem + Family-History: „Entry gut, Exit schlecht")
   und die Aufgabe, Komponenten über Linien hinweg zu rekombinieren.
   Alle bestehenden Gates (Prior-Art, `validate_hypothesis`, Critic aus WP3)
   gelten unverändert.
2. **Daten-Scan-Tool** — deterministischer Scanner (kein LLM):
   konditionale Return-Statistiken über die Dukascopy-Historie
   (z. B. Return nach 2σ-Move, gruppiert nach Session/Wochentag/
   Vol-Regime), als Researcher-Tool `scan_anomalies(…)` exponiert.
   **Harte Overfitting-Guards**: Scan ausschließlich auf dem
   In-Sample-Fenster (vor dem Holdout-Cutoff aus `holdout_months`,
   Plan 009 WP4); jede aus einem Scan geborene Hypothese erhöht die
   WP2-Trial-Zählung ihrer Familie um die Anzahl gescannter Zellen
   (sonst ist der Scanner eine Multiple-Testing-Maschine am Gate vorbei).
   Der Researcher-Prompt verlangt für Scan-Funde einen plausiblen
   Mechanismus („warum existiert das?") — Anomalie ohne Mechanismus wird
   verworfen.
3. → verifizieren: Scanner-Unit-Tests mit synthetischen Candles (bekannte
   eingebaute Anomalie wird gefunden, Holdout-Fenster nie gelesen);
   Crossover-E2E mit Stub-Modell.

---

## STOP conditions

1. **WP1/1**: Wenn im Produktions-Workspace außer `forexsb` noch weitere
   Quellen konfiguriert sind oder unklar ist, ob die forexsb-CSVs noch
   gebraucht werden → STOP, Maintainer fragen. Niemals Daten löschen, nur
   verschieben.
2. **WP2/2**: Wenn die DSR-Implementierung das Zahlenbeispiel aus
   Bailey/López de Prado (2014) nicht innerhalb ±0.01 reproduziert → STOP,
   nicht „ungefähr passend" ins Gate schrauben.
3. **WP5/1**: Regime-Quelle (Option A vs. B) ist eine Cross-Repo-Entscheidung
   → vor Implementierung Maintainer-OK einholen.
4. **Generell**: Lifecycle-Übergänge ausschließlich über
   `orchestrator/lifecycle.py`; keine DELETE-Endpoints; Prompt-Änderungen
   dürfen die harten Regeln (Anti-Redundanz, Median-Gate, Phasen-Funnel)
   nicht aufweichen. Kein Commit von `uv.lock`. PRs mit `haexhub`-Account.

## Verifikation gesamt

- `uv run pytest -q` (fwbg-agents) grün nach jedem WP; fwbg-Testsuite grün
  nach WP1/2 und ggf. WP5-Option A.
- Nach WP1: ein voller Research-Brief-Lauf end-to-end (Researcher →
  Translator → Backtest) ohne datasource-Fehler, `datasource='dukascopy'`
  im persistierten `strategy.json`.
- Nach WP2: `promote_gate_results.json` einer Test-Strategie enthält
  `n_trials` + `dsr`.
- Nach WP4: Dashboard-Run-Detail zeigt `analyst_query`-Events eines echten
  Analyst-Laufs.
