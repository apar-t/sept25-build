"""Run one full tick: ingest (lane A) then agent (lane B). This is the demo's "run tick now".

    uv run scripts/tick.py            # fetch every vendor with Nimble, then run the agent
    uv run scripts/tick.py --no-fetch # agent only, on whatever is already in nw_snapshots
    uv run scripts/tick.py --agent    # + the tool-using agent (investigates open questions)
    uv run scripts/tick.py --takeover # run even if a long-running runner holds the lease
    uv run scripts/tick.py --narrate  # print what the agent does, step by step (demo)
"""

import getpass
import socket
import sys
import time
from datetime import datetime, timezone

from sept25_build import agent
from sept25_build.agent.runner import LEASE_SECS, Lease
from sept25_build.agent import narrate
from sept25_build.contracts import now

narrate.enabled = "--narrate" in sys.argv

# Read-only lease check: a manual tick next to a live runner would process the same snapshots twice.
if "--takeover" not in sys.argv:
    cur = Lease("rawtree", f"{getpass.getuser()}@{socket.gethostname()}", "live").current()
    if cur and cur.get("state") == "running":
        at = datetime.fromisoformat(cur["at"].replace(" ", "T"))
        age = (now() - (at if at.tzinfo else at.replace(tzinfo=timezone.utc))).total_seconds()
        if age < LEASE_SECS:
            sys.exit(f"a runner is live: {cur['holder']} (run {cur.get('run_id')}, heartbeat {age:.0f}s ago). "
                     f"Stop it, wait {LEASE_SECS - age:.0f}s, or pass --takeover.")

if "--no-fetch" not in sys.argv:
    from sept25_build import ingest
    if not hasattr(ingest, "run_once"):
        sys.exit("ingest.run_once() isn't there yet (lane A). Use --no-fetch.")
    t0 = time.monotonic()
    narrate.say("ingest: fetching every vendor's sub-processor list and terms (lane A, Nimble)")
    got = ingest.run_once()
    narrate.say(f"ingest: {len(got) if isinstance(got, list) else '?'} snapshot(s) written in {time.monotonic() - t0:.1f}s")

alerts = agent.run_tick(agent="--agent" in sys.argv)
for a in alerts:
    print(f"{'VIOLATION' if a.kind == 'violation' else 'resolved '} {a.rule}  {a.title}")
print(f"{len(alerts)} alert(s)")
