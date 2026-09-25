"""Record the stage console's view of a finished run into web/demo.json, for the public replay page (web/demo.html).

    NW_TABLE_SUFFIX=_e2e uv run python web/export_demo.py

Reads RawTree only (read-only SQL). Rebuilds lane B's /api/state as it looked after each check by dropping rows
with a later tick, so the replay shows exactly what the live console showed. No keys end up in the file.
"""

import json
import os
import re
import sys
from pathlib import Path

from sept25_build import rawtree
from sept25_build.agent import console

# Check numbers in the recorded run: the state before step 1, then the check each demo step triggered.
BASELINE, STEP_TICKS = 2, [3, 4, 5, 6]
OUT = Path(__file__).with_name("demo.json")
TUNNEL = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")  # the quick tunnel is gone after the demo

_raw, _cache, cut = rawtree.query, {}, [None]


def _query(sql: str) -> list[dict]:
    if sql not in _cache:
        _cache[sql] = _raw(sql)
    rows = _cache[sql]
    return rows if cut[0] is None else [r for r in rows if "t" not in r or console._int(r["t"]) <= cut[0]]


def state_at(tick: int) -> dict:
    cut[0] = tick
    return {"at": f"check #{tick}", "vendors": console._vendors(), "alerts": console._alerts(),
            "episodes": console._episodes(), "memory_ops": console._memory_ops(), "series": console._series()}


def main() -> None:
    if not os.environ.get("NW_TABLE_SUFFIX"):
        sys.exit("set NW_TABLE_SUFFIX (e.g. _e2e) so this reads a rehearsal run, not the stage tables")
    rawtree.query = _query
    ticks = _query(f"SELECT toString(tick) t, toString(agent) a, toString(latency_ms) l FROM {console.TABLES['ticks']}")
    secs = {console._int(r["t"]): console._int(r["l"]) / 1000 for r in ticks if r["a"] == "nights_watch"}
    states = [state_at(t) for t in [BASELINE] + STEP_TICKS]
    steps = [{"tick": t, "seconds": round(secs.get(t, 0), 1),
              "alerts": [a for a in s["alerts"] if a["tick"] == t]} for t, s in zip(STEP_TICKS, states[1:])]
    data = {"suffix": console.SUFFIX, "states": states, "steps": steps, "proof": console._proof()}
    text = TUNNEL.sub("https://demo-mirror", json.dumps(data, default=str, separators=(",", ":")))
    OUT.write_text(text)
    print(f"wrote {OUT} ({len(text) // 1024} KB): baseline #{BASELINE}, steps {STEP_TICKS}")


if __name__ == "__main__":
    main()
