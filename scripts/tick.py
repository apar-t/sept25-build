"""Run one full tick: ingest (lane A) then agent (lane B). This is the demo's "run tick now".

    uv run scripts/tick.py            # fetch every vendor with Nimble, then run the agent
    uv run scripts/tick.py --no-fetch # agent only, on whatever is already in nw_snapshots
"""

import sys

from sept25_build import agent

if "--no-fetch" not in sys.argv:
    from sept25_build import ingest
    if not hasattr(ingest, "run_once"):
        sys.exit("ingest.run_once() isn't there yet (lane A). Use --no-fetch.")
    ingest.run_once()

alerts = agent.run_tick()
for a in alerts:
    print(f"{'VIOLATION' if a.kind == 'violation' else 'resolved '} {a.rule}  {a.title}")
print(f"{len(alerts)} alert(s)")
