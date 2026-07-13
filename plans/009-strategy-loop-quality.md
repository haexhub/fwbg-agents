# Plan 009: Strategie-Loop-Qualität — Diagnostik, Anti-Overfitting, Funnel, Spec

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md`.
>
> **Drift check (run first)**:
> `git -C ~/Projekte/fwbg-agents log --oneline -8 -- src/fwbg_agents/agents/analyst.py src/fwbg_agents/agents/runner.py src/fwbg_agents/agents/researcher.py src/fwbg_agents/orchestrator/recommendations.py src/fwbg_agents/orchestrator/lineage.py src/fwbg_agents/orchestrator/prior_art.py src/fwbg_agents/config.py`
> Zeilenanker unten gegen den Live-Code prüfen. Bei Abweichung: Anker neu
> suchen, bevor du editierst; bei struktureller Abweichung STOP.

## Status

- **Status**: IN PROGRESS — WP1 + WP2 + WP3 + WP4 DONE. Offen: WP5, WP6.
  - **WP3 DONE**: `config` reiterate_max_depth 5→12, `universe_narrowing_min_iteration=5`,
    `universe_min_size=3`. `ResearcherHypothesis` + `asset_specific`/`asset_specific_rationale`;
    `validate_hypothesis` erzwingt ≥3 Assets in Erstiteration (asset_class-Scope zählt als breit)
    außer asset_specific (dann Rationale-Pflicht); researcher.md Regel 4 erweitert. asset_specific
    wird in `strategy.metadata_json` persistiert. `lineage.render_family_history` rendert zusätzlich
    Per-Asset-Sharpe-Serie über die Kette. Analyst-Prompt: Phasen-Funnel-Regeln (Slots
    `universe_narrowing_min_iteration`/`universe_min_size`). Deterministische Hard-Rules in
    `recommendations._enforce_universe_rules`: (1) keine Verengung vor Phasengrenze außer errored,
    (2) nie unter universe_min_size außer asset_specific, (3) target_assets ⊆ Backtest-Universum
    → `RecommendationRejectedError`.
  - **WP4 DONE** (cross-repo). fwbg (Branch `feat/009-strategy-loop-quality`,
    Commit 461b46c): `StrategyConfig` + CLI + `POST /api/runs/start` bekommen
    `start_date`/`end_date`/`cost_multiplier`; `process.py` slict die Daten aufs
    Fenster (Fold-Logik positional → unverändert), `context.py` skaliert Spread
    um `cost_multiplier`. Spike 4.1 ergab: beide nur kleine Patches, KEIN STOP.
    Mit echtem Run verifiziert (2025-Fenster 60000→24900 Bars). fwbg-agents:
    `config.holdout_months=24`; Runner-Iterations-Backtests enden bei
    `today - holdout_months` (`_months_ago_iso`); `FwbgClient.start_run` +
    Runner-`_execute_backtest` threaden die Parameter durch; neues
    `orchestrator/promote_gate.py` fährt sequentiell Holdout- + Kosten-Stress-Run,
    prüft gegen neue Criteria-Sektionen `promote_holdout`/`promote_cost_stress`
    (`lifecycle.check_criteria_section`), schreibt `promote_gate_results.json`
    (kumulatives `fail_count`) + `promote_gate_*`-Events. `validate_and_apply`
    (promote) ruft das Gate; Fail → bleibt BACKTESTED. Analyst-Prompt bekommt
    `{{ promote_gate }}`-Slot (2. Fail → abandon/fundamentale Änderung).
  - **WP1 DONE**: neues `orchestrator/trade_diagnostics.py` (liest per-Trade aus
    `fold_results.json` `fold_details[].test_trades_detail` + surft fwbgs
    `trade_analytics` durch — KEINE fwbg-Änderung nötig, da alles auf Platte);
    Runner schreibt `trade_diagnostics.md`-Sidecar (non-fatal); Analyst konsumiert
    via `{{ trade_diagnostics }}`-Slot + Entscheidungshilfe im Prompt. Buckets:
    Stunde/Wochentag/Haltedauer-Quartile, Jahres-Segmente, Payoff/Verlustserie/
    Top-5-Klumpen. Gegen echten Run (diag_orb_003) verifiziert.
  - **WP2 DONE**: Median-Gate. Drift-Befund: das Gate liegt real in
    `lifecycle._guard_strategy_backtested_to_paper` (via `backtest_metrics`-Payload),
    NICHT in `validate_and_apply` wie im Plan angenommen — Feeder api/runs.py +
    auto_runner.py + Runner-Payload auf `_median_metrics_across_assets` umgestellt;
    Analyst-Prompt zeigt Median (gated) + Best-Symbol (info).
- **Priority**: P1 (WP1, WP2, WP4 = Kern) / P2 (WP3, WP5) / P3 (WP6)
- **Effort**: L gesamt (WP2 = S; WP1, WP3, WP5 = M; WP4 = M cross-repo; WP6 = S Spike + M)
- **Risk**: MED — WP4 braucht fwbg-seitige API-Erweiterungen (Zeitraum-/Kostenparameter);
  Rest ist additiv in fwbg-agents.
- **Repos**: `fwbg-agents` (alles), `fwbg` (WP4 Backtest-Parameter, ggf. WP1 Trade-Felder)
- **Depends on**: — (Plan 001–008 sind gemergt bzw. abgeschlossen)

## Ziel & Motivation (mit User diskutiert 2026-07-13)

Der Loop Researcher → Translator → Runner → Analyst → reiterate findet bisher
keine profitablen Strategien. Diagnose aus dem Review:

1. Der Analyst sieht nur aggregierte Metriken pro Asset → die Wahl des
   Iterations-Hebels (`tune_params` vs. `change_exit` vs. `modify_plugins`)
   ist weitgehend Raten.
2. Das Promotion-Gate prüft die Metriken des **besten** Symbols
   (`_best_symbol_metrics_from_results`) → Selection Bias.
3. Jede Iteration wird auf denselben Daten bewertet, auf denen entschieden
   wird → die Iterationskette ist ein Multiple-Testing-Problem, das aktuelle
   `mc_pvalue`-Gate korrigiert dafür nicht.
4. Hypothesen werden nur end-to-end (volle Strategie) getestet → tote Ideen
   verbrennen Iterationen am Exit-Tuning.
5. `strategy_family` ist Freitext → der Same-Family-Bypass der
   Anti-Redundanz greift nicht (real beobachtet: zwei fast identische
   Familien als verschiedene Strings in der DB).

**User-Entscheidungen** (verbindlich für diesen Plan):

- Nur **ein Backtest-Run at a time** (Rechnerauslastung) — kein Fan-out.
  Holdout/Kosten-Stress laufen sequentiell und nur am Promote-Tor.
- Universum-Verengung als Phasen-Funnel: erst alle Assets gemeinsam
  optimieren, dann evidenzbasiert verengen (Details WP3).
- Single-Asset-Strategien: Hybrid-Regel — Erstiteration braucht ≥3 Assets,
  AUSSER die Hypothese ist explizit asset-spezifisch (Flag + Begründung);
  dann ist Single-Asset erlaubt und die Verengungslogik entfällt.
- Speckit-Pattern (wie bei Plugins) auf Strategien übertragen (WP5).

## Ausführungsreihenfolge

WP1 → WP2 → WP4 → WP3 → WP5 → WP6. WP1 und WP2 sind unabhängig voneinander;
WP3 setzt WP1 (Per-Asset-Historie) und WP2 (Median-Gate) voraus. WP4 ist
unabhängig, aber cross-repo — den fwbg-Spike (Schritt 4.1) früh machen, damit
die fwbg-Erweiterung parallel zu WP1–WP3 landen kann. WP6 zuletzt (explorativ).

---

## WP1 — Trade-Diagnostik für den Analyst (P1, M)

**Ziel**: Der Analyst bekommt pro Backtest ein deterministisch berechnetes
`trade_diagnostics.md`, das den Failure-Mode zeigt, statt ihn aus
Sharpe/PF-Aggregaten raten zu müssen.

**Datenquelle (verifiziert)**: fwbg schreibt Run-Verzeichnisse unter
`settings.fwbg_test_results_dir` (`~/fwbg/test_results/<run>/`) mit
`grid_details/<symbol>/trades.json` und `unified_metrics.json` — der
Calibrator liest beides schon (`agents/calibrator.py:70-89`).

### Schritt 1.1 — Spike: trades.json-Schema erheben

Einen realen Run unter `~/fwbg/test_results/` öffnen und die Felder eines
Trade-Eintrags dokumentieren (in den PR-Text bzw. als Kommentar in das neue
Modul). Relevant: Entry-/Exit-Zeitpunkt, P&L, Richtung, und ob
MAE/MFE-artige Felder (max adverse/favorable excursion, high/low während
des Trades) vorhanden sind.

- Verifikation: Schema-Notiz existiert; mindestens P&L + Timestamps sind da.
- Falls MAE/MFE fehlt: Diagnostik OHNE MAE/MFE bauen (immer noch wertvoll)
  und eine fwbg-Erweiterung als optionalen Folgepunkt notieren — **kein STOP**.
- Falls gar kein trades.json für Runner-Backtests entsteht (nur für
  Grid-Runs): STOP → fwbg muss Trades auch für normale Runs emittieren.

### Schritt 1.2 — `orchestrator/trade_diagnostics.py`

Neues Modul, rein deterministisch (kein LLM), Input: Run-Verzeichnis +
Symbolliste. Output: `TradeDiagnostics`-Pydantic-Modell + Markdown-Renderer.
Pro Symbol und aggregiert:

- P&L-Buckets nach **Tagesstunde**, **Wochentag**, **Haltedauer-Quantilen**
  (Expectancy + Trade-Count je Bucket)
- **Equity-Segmente pro Jahr** (Return, MaxDD, Trade-Count je Jahr — zeigt
  „Edge nur 2020?")
- **Win/Loss-Verteilung**: Payoff-Ratio, längste Verlustserie,
  Anteil der Top-5-Trades am Gesamt-P&L (Klumpenrisiko)
- Falls MAE/MFE vorhanden: MAE/MFE-Quartile getrennt für Gewinner/Verlierer
  (Kernfrage: „liefen Verlierer erst ins Plus?" → Exit-Problem vs.
  „sofort ins Minus?" → Entry-Problem)

Kein Regime-Split in dieser Ausbaustufe (bräuchte Marktdaten-Zugriff —
bewusst rausgelassen, Einfachheit zuerst).

- Verifikation: Unit-Tests mit synthetischen trades.json-Fixtures
  (`tests/orchestrator/test_trade_diagnostics.py`), inkl. leerer/defekter
  Input → degradiert zu „(keine Trade-Daten)" statt Exception.

### Schritt 1.3 — Runner schreibt Sidecar, Analyst konsumiert

- `agents/runner.py` (nach `results_path.write_text`, ~L283): Diagnostik aus
  dem fwbg-Run-Verzeichnis berechnen, `trade_diagnostics.md` neben
  `fwbg_results.json` schreiben. Fehler dabei sind non-fatal (Warning) —
  ein Backtest ohne Diagnostik bleibt gültig.
- `agents/analyst.py` (`analyze`, ~L438): Sidecar lesen (fehlend →
  Platzhalter) und via neuem `{{ trade_diagnostics }}`-Slot in den Prompt.
- `agents/prompts/analyst.md`: neuer Abschnitt „## Trade-Diagnostik" +
  Entscheidungshilfe im Kopfteil: MAE zeigt Gewinner-vor-Verlust →
  `change_exit`; Verluste sofort → Entry/Filter (`modify_plugins`);
  Edge nur in bestimmten Stunden/Tagen → Session-Filter; Klumpen-P&L →
  Robustheit anzweifeln.

- Verifikation: bestehende Analyst-Tests grün + neuer Test, dass der Slot
  gerendert wird; `uv run pytest tests/agents/ tests/orchestrator/ -q`.

---

## WP2 — Median-Gate statt Best-Symbol (P1, S)

**Ziel**: Das Promotion-Gate bewertet die Strategie über ihr Universum,
nicht über das zufällig beste Symbol.

### Schritt 2.1 — Aggregatmetriken

In `agents/runner.py` (`_best_symbol_metrics`-Pfad, ~L157) und
`agents/analyst.py` (`_best_symbol_metrics_from_results`, L306):
zusätzlich `_median_metrics_across_assets(run)` einführen — pro Metrik der
Median über alle Assets mit Ergebnissen (bei `min_trades`: Median der
Per-Asset-Counts; bei einem einzigen Asset ist der Median identisch →
Single-Asset-Strategien funktionieren unverändert).

Das Gate (`check_backtest_criteria`-Aufrufe in `orchestrator/recommendations.py`
`validate_and_apply` und im Runner-Transition-Payload) wechselt auf die
Median-Metriken. Die Best-Symbol-Metriken bleiben als Zusatzinfo im
Analyst-Prompt (Umbenennung des Prompt-Abschnitts: „best-performing symbol
(informativ — das Gate prüft den MEDIAN)").

- Verifikation: neue Unit-Tests: 1 Asset top / 4 Assets schlecht → Gate
  FAIL (vorher PASS); homogen gutes Universum → PASS; Single-Asset
  unverändert. Bestehende Tests anpassen, die Best-Symbol-Gating annehmen.

### Schritt 2.2 — Analyst-Prompt konsistent machen

`agents/prompts/analyst.md` L121-124: Abschnitt „Backtest metrics
(best-performing symbol — the promotion gate checks these)" umformulieren
(Gate = Median). Regel 7 (per-Asset-Urteil) bleibt.

- Verifikation: `grep -n "promotion gate" src/fwbg_agents/agents/prompts/analyst.md`
  zeigt nur noch die Median-Formulierung.

---

## WP3 — Phasen-Funnel + historienbasierte Universum-Verengung (P2, M)

**Ziel**: Erst breit optimieren, dann evidenzbasiert verengen — mit
deterministischer Durchsetzung, nicht nur Prompt-Bitte.

### Schritt 3.1 — Config

`config.py`:
- `reiterate_max_depth` Default 5 → **12** (Feld L132; `le=20` reicht).
- Neu: `universe_narrowing_min_iteration: int = 5` (ab dieser Iteration darf
  verengt werden), `universe_min_size: int = 3` (Boden der Verengung).

### Schritt 3.2 — Mindest-Universum + `asset_specific`-Flag (Erstiteration)

- `orchestrator/hypotheses.py` (`ResearcherHypothesis`): neues Feld
  `asset_specific: bool = False` + `asset_specific_rationale: str = ""`.
- `validate_hypothesis`: wenn `asset_specific=False` →
  `len(suggested_universe) >= 3` erzwingen (Reject mit klarer Message, der
  Researcher-Fanout versucht es erneut); wenn `True` → Rationale-Pflicht
  (nicht leer), Single-Symbol erlaubt.
- `agents/prompts/researcher.md` Regel 4 erweitern: Default ≥3 Assets;
  `asset_specific` nur, wenn der Edge mechanisch an ein Instrument gebunden
  ist (z.B. DAX-Eröffnungsauktion), mit Begründung.

- Verifikation: Tests in `tests/orchestrator/` für beide Zweige.

### Schritt 3.3 — Per-Asset-Historie in der Family-History

`orchestrator/lineage.py` (`render_family_history`, L176): pro Iteration
zusätzlich eine kompakte Per-Asset-Zeile rendern
(`EURUSD: sharpe 0.3 → 0.4 → 0.2`), Quelle: `fwbg_results.json` je
Iterations-Verzeichnis (analog zur bestehenden Metrik-Extraktion L132).

- Verifikation: Lineage-Tests mit 3-Iterationen-Fixture; Ausgabe enthält
  Per-Asset-Serien.

### Schritt 3.4 — Phasenregeln im Analyst-Prompt + deterministische Durchsetzung

- `agents/prompts/analyst.md`: Phasenansage — Iteration <
  `universe_narrowing_min_iteration`: `target_assets` NICHT verwenden
  (Ausnahme: Assets, die fwbg als errored markiert); danach: Verengung
  erlaubt, aber nur für Assets, die **über ≥2 aufeinanderfolgende
  Iterationen** klar unter dem Median liegen oder durchgehend negative
  Expectancy haben (Belege aus der Per-Asset-Historie zitieren); nie unter
  `universe_min_size` Assets (außer `asset_specific`). Entscheidungsregel
  nach Phase 1: gibt es Assets, die die Kriterien einzeln bestehen →
  auf diese fokussieren; sonst nur klar abgeschlagene Assets droppen.
- `orchestrator/recommendations.py` (`validate_and_apply`, L55):
  Hard-Rules — `target_assets` vor der Phasengrenze → Recommendation
  ablehnen (wie bestehende Guard-Mechanik); Verengung unter
  `universe_min_size` → ablehnen (außer Parent ist `asset_specific`);
  `target_assets` ⊄ Parent-Universum → ablehnen.

- Verifikation: Tests für alle drei Hard-Rules; bestehende
  recommendations-Tests grün.

---

## WP4 — Promote-Tor: Holdout + Kosten-Stresstest (P1, M, cross-repo)

**Ziel**: Ein `promote` wird erst wirksam, wenn die Strategie (a) auf einem
nie zur Iteration benutzten Zeitfenster und (b) unter 2× Kosten besteht.
Beides sequentielle Einzelläufe → kompatibel mit „ein Run at a time".

### Schritt 4.1 — Spike: fwbg-Fähigkeiten (FRÜH machen, blockiert 4.2+)

`FwbgClient.start_run` (tools/fwbg_client.py:111) kennt heute **nur**
`strategy_name`/`asset_classes`/`assets`/`description` — keine
Zeitraum-, keine Kostenparameter; auch `strategy.json` trägt nichts davon
(Translator/Validator greppen leer). Im fwbg-Repo klären
(`docs/plans/2026-06-23-fwbg-agents-design.md` + fwbg-API-Code):

1. Kann `POST /api/runs/start` einen Datumsbereich? Falls nein → fwbg-seitig
   `start_date`/`end_date` ergänzen.
2. Gibt es Spread-/Slippage-/Kommissionsparameter (pro Run oder in der
   Strategie-Config)? Falls nein → fwbg-seitig einen
   Kostenmultiplikator (`cost_multiplier`) ergänzen.

**STOP** nach dem Spike und Befund berichten, wenn eine der Erweiterungen
ein größerer fwbg-Umbau wäre — Umfang dann mit dem Maintainer abstimmen.

### Schritt 4.2 — Iterations-Backtests enden an der Holdout-Grenze

- `config.py`: `holdout_months: int = 24`.
- `agents/runner.py`: reguläre Backtests mit
  `end_date = today - holdout_months` starten. Damit hat KEINE Iteration
  die letzten 2 Jahre je gesehen.
- Achtung `min_trades >= 300`: das Fenster schrumpft — prüfen, ob die
  Kriterien-Defaults dazu passen (Calibrator-Doku), sonst im PR notieren.

### Schritt 4.3 — Promote-Gate-Läufe

In `orchestrator/recommendations.py` (`validate_and_apply`), Zweig
`promote`, NACH dem bestehenden Kriterien-Check und VOR der Transition:

1. **Holdout-Run**: gleiches Universum (ggf. verengt), Zeitraum
   `[today - holdout_months, today]`. Bestehen: positiver Total Return,
   `sharpe >= 1.0`, `profit_factor >= 1.3`, `min_trades >= 60`
   (Startwerte — bewusst milder als das Haupt-Gate, da kürzeres Fenster;
   als eigene Sektion in den Criteria-Defaults ablegen, nicht hart im Code).
2. **Kosten-Stress-Run**: volles Fenster, `cost_multiplier = 2.0`.
   Bestehen: positiver Total Return und `profit_factor >= 1.2`.

Ergebnis-Handling: beide bestanden → Transition wie bisher. Einer
durchgefallen → Promote wird NICHT ausgeführt; Ergebnisse als
`promote_gate_results.json`-Sidecar persistieren, Transition-Reason
dokumentiert den Fail, und die Strategie bleibt BACKTESTED — der nächste
Analyst-Lauf bekommt die Gate-Ergebnisse in den Prompt (kleiner neuer
Prompt-Slot) und entscheidet iterate/abandon. Zweiter Promote-Fail derselben
Strategie → Analyst-Prompt sagt explizit: abandon oder fundamentale Änderung.

- Verifikation: Tests mit Fake-FwbgClient für beide Fail-Pfade + Pass-Pfad;
  Event-Emission (`promote_gate_*`) für die Timeline (Pattern aus Plan 008).

---

## WP5 — StrategySpec + kontrolliertes Vokabular + Lessons-Digest (P2, M)

**Ziel**: Semantische Gleichheit von Strategien erkennbar machen (Speckit-
Pattern wie bei Plugins, wo `capability` der Dedup-Anker ist) und das
Gelernte aus Abandons global verfügbar machen.

### Schritt 5.1 — `speckit/strategy_spec.py`

`StrategySpec`-Modell analog `PluginSpec` (speckit/spec.py):

- `edge_mechanism: str` — genau EIN Satz, der Dedup-Anker
- `entry_logic`, `exit_mechanism`, `regime_assumption`, `filters`,
  `timeframe`, `universe` — die Differenzierungs-Dimensionen, die
  researcher.md Regel 1 heute schon als Prosa verlangt
- `strategy_family: StrategyFamilyLit` — **kontrolliertes Vokabular**
  (Literal-Enum: `ORB`, `mean_reversion`, `momentum`, `breakout`,
  `carry`, `seasonality`, `liquidity_sweep`, `volatility`, `pairs`,
  `other` — beim Umsetzen mit den real existierenden DB-Werten abgleichen)
- `asset_specific: bool` (aus WP3)

### Schritt 5.2 — Researcher emittiert den Spec

- `ResearcherHypothesis` um die Spec-Felder erweitern (bzw. Spec aus der
  Hypothese generieren, Pattern `speckit/spec_generator.py`);
  `strategy_family` wird dadurch validiert statt Freitext.
- `research_flow.py`: `spec.md` (gerenderter StrategySpec) neben
  `hypothesis.json` in `iteration_001/` schreiben; `Strategy.strategy_family`
  bekommt den Enum-Wert.
- Bestands-Strategien: kein Migrations-Zwang — `lookup_prior_art` behandelt
  unbekannte Alt-Familien wie bisher (String-Vergleich), nur neue Strategien
  sind enum-validiert. (Analog `scripts/backfill_plugin_specs.py` KANN ein
  Backfill-Skript folgen — nicht Teil dieses Plans.)

### Schritt 5.3 — Prior-Art v2

`orchestrator/prior_art.py`: `PriorArtMatch` um `edge_mechanism` (aus dem
Spec des Treffers, falls vorhanden) erweitern, damit der Researcher beim
Differenzieren den Anker-Satz des Prior-Art-Treffers sieht statt nur Tags.
Same-Family-Match wird durch das Enum zuverlässig. (Embedding-Layer-2
bleibt bewusst draußen — erst validieren, ob Enum + Anker-Satz reichen.)

### Schritt 5.4 — Lessons-Digest

- Neues Modul `orchestrator/lessons.py`: nach jedem Abandon deterministisch
  `data/lessons.md` regenerieren — Aggregation aller `lessons`-Listen aus
  den Abandon-Post-Mortems, gruppiert nach `strategy_family`, mit Datum
  und Slug. Kein LLM.
- `research_flow.py` / `researcher.py`: Digest (länggekappt, z.B. 4000
  Zeichen, neueste zuerst) als neuen Prompt-Slot `{{ lessons_digest }}` in
  researcher.md injizieren („Lehren aus abandonten Strategien — NICHT
  wiederholen").

- Verifikation WP5 gesamt: Spec-Roundtrip-Tests, validate_hypothesis lehnt
  unbekannte Familie ab, lessons.md-Generierung aus Fixture-Post-Mortems,
  Prompt-Slot gerendert. `uv run pytest -q` komplett grün.

---

## WP6 — Event-Study-Vorfilter (P3, S Spike + M)

**Ziel**: Rohen Edge billig falsifizieren, BEVOR die volle Kette
(Translator → Publish → Backtest → Analyst-Iterationen) anläuft.

### Schritt 6.1 — Spike

Klären (fwbg-Repo + 1 Testlauf): lässt sich eine Minimal-Strategie bauen —
nur Entry-Signal + Zeit-Exit nach n Bars, ohne Filter/Sizing — die als
Event-Study taugt (Expectancy des rohen Signals)? Erwartung: ja, da der
Translator Exits frei komponiert. Falls nein: lokales Skript über die von
fwbg heruntergeladenen OHLCV-Daten als Alternative bewerten. Befund
berichten, Umsetzungsvariante festlegen — **STOP-Punkt zur Abstimmung**.

### Schritt 6.2 — Umsetzung (nach Spike-Entscheid)

- `research_flow.py`: zwischen Hypothese-Persistierung und vollem
  Translator-Lauf den Event-Study-Run schalten (sequentiell, ein Run).
- Abbruchregel: Expectancy des rohen Signals ≤ 0 auf ALLEN Assets des
  vorgeschlagenen Universums → Strategie direkt abandonen mit Post-Mortem
  „raw edge absent" + Lesson (füttert WP5-Digest). Sonst: normale Kette.
- Ergebnis als `event_study.json` + Abschnitt in `research_notes.md`.

---

## STOP conditions

1. **WP1**: Runner-Backtests erzeugen kein `grid_details/<symbol>/trades.json`
   (nur Grid-Runs tun das) → fwbg-Erweiterung nötig, Umfang abstimmen.
2. **WP4**: fwbg kann weder Datumsbereich noch Kostenparameter, und die
   Ergänzung ist kein kleiner Patch → Umfang mit Maintainer abstimmen.
3. **WP2**: Falls `check_backtest_criteria` inzwischen an mehr Stellen hängt
   als Runner-Payload + `validate_and_apply` (Drift) → erst Karte machen.
4. Jede nötige DB-Migration außer additiven Spalten (z.B. für
   `asset_specific`) → kurz abstimmen.
5. `plans/README.md`-Statuszeile nach jedem abgeschlossenen WP aktualisieren,
   nicht erst am Ende.

## Nicht in Scope (bewusst)

- Fan-out / parallele Kind-Iterationen (User: ein Run at a time).
- Embedding-basierte Prior-Art (erst Enum + Anker-Satz validieren).
- Regime-Splits in der Trade-Diagnostik (bräuchte Marktdaten-Join).
- Backfill der Alt-Strategien auf StrategySpec.
- Deflated-Sharpe-/Trial-Count-Korrektur: Holdout (WP4) adressiert dasselbe
  Risiko billiger; Trial-Zähler nur nachrüsten, falls Holdout-Fails zeigen,
  dass In-Sample-Iterationen systematisch overfitten.
