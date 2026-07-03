# Preset-Kristallisierung: bewährte Pipelines → benannte Presets mit Provenienz

**Status:** Geplant (noch nicht begonnen)
**Kontext:** Folgt auf M7 (Live-Katalog + Inline-Komposition, PR feat/live-catalog-inline-composition) und fwbg#44 (Preset-Seeding).

## Problem

Seit M7 komponiert der Translator `pipeline`/`model`/`filters` pro Strategie inline aus dem Live-Plugin-Katalog. Die geseedeten Presets sind handgemachte, wissenschaftlich unvalidierte Startpunkte — nur für Menschen (Dashboard-Editor) gedacht. Was fehlt: ein Weg, wie sich **bewährte** komponierte Konfigurationen zu wiederverwendbaren, benannten Presets verdichten — mit nachvollziehbarer Herkunft statt Willkür.

## Idee

Analog zum Plugin-Lifecycle (`specified → authored → verified → adopted_in_fwbg`): Eine inline komponierte Pipeline, deren Strategie eine Bewährungsschwelle erreicht, wird als Preset „kristallisiert".

## Design-Skizze

1. **Trigger:** Strategie erreicht `paper_trading` (konfigurierbar; alternativ erst `live_trading`). Der Orchestrator prüft, ob die inline `pipeline` (und optional `model`) bereits als Preset existiert (struktureller Vergleich, nicht Namensvergleich).
2. **Kristallisieren:** `POST /api/presets/pipelines` (fwbg) mit
   - Namensschema `{strategy_family}_{NNN}_v1` (kollisionfrei, nie überschreiben — wie beim Strategie-Publish),
   - `_meta.description` = Kurzform der Hypothese,
   - **Provenienz-Metadaten** in `_meta`: `source_strategy_slug`, `source_hypothesis_title`, `evidence` (Sharpe/Trades/Zeitraum aus der Transition-Payload `backtest_metrics` bzw. Paper-Telemetrie), `crystallized_at`, `created_by: "agents"`.
3. **Sichtbarkeit:** Der Live-Katalog (fetch_live_catalog) liefert die Preset-Listen bereits — kristallisierte Presets erscheinen automatisch im Dashboard-Editor. Der Translator braucht sie NICHT als Auswahl (er komponiert weiter inline); sie dienen Menschen und ggf. späteren Vergleichs-/Priorart-Checks.
4. **fwbg-Seite:** Preset-Create-Endpoint existiert (`/api/presets/{section}`), muss aber Provenienz-Metadaten in `_meta` durchlassen (prüfen: `PresetMeta` erweitern oder freie Zusatzfelder erlauben).
5. **Audit:** Transition-Row am Strategy-Datensatz (`reason: "preset crystallized: <name>"`, Payload mit Preset-Name + Evidenz).

## Abgrenzung

- Keine automatische Wiederverwendung durch den Translator (bewusst — sonst konvergiert die Forschung auf frühe Gewinner; Exploration bleibt inline).
- Kein Überschreiben/Versionieren bestehender Presets durch Agents; neue Namen only.
- Regime-Filter/Grids/Exit-Params ausgenommen, bis der Bedarf konkret wird.

## Offene Fragen

- Schwelle: reicht `paper_trading`-Eintritt oder erst Paper-Analyst-Empfehlung (`paper_analyst_promote_recommended`)?
- Soll der Researcher kristallisierte Presets als „prior art" sehen (Anti-Redundanz-Signal)?
- Dashboard: Badge „agent-crystallized" im Preset-Editor?
