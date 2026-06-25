"""SUPERSEDED — the M5b PluginAuthor was retired in M5d.

This smoke originally drove the single-agent PluginAuthor + PluginEvaluator
chain. M5d replaced PluginAuthor with a Planner+Implementer split; the
author + evaluate end-to-end path is now covered by:

    scripts/m5c_smoke.py    — author (M5d) → evaluate → reiterate-with-plugin
    scripts/m5d_smoke.py    — author (M5d) → evaluate, with planner/implementer
                              AgentRun + LlmCall assertions

Kept as a stub for traceability ([[feedback-no-hard-delete]]). Running it does
nothing useful.
"""

from __future__ import annotations

import sys


def main() -> int:
    print(
        "[m5_smoke] SUPERSEDED — see scripts/m5c_smoke.py or scripts/m5d_smoke.py",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
