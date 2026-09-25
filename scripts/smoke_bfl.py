"""Check the BFL key (free), then generate one test image with --generate (~$0.03).

    uv run scripts/smoke_bfl.py
    uv run scripts/smoke_bfl.py --generate
"""

import sys
import time

from sept25_build import bfl

print(f"BFL credits: {bfl.credits()}")

if "--generate" in sys.argv:
    t = time.monotonic()
    path = bfl.generate(
        "isometric diagram of an AI agent's memory: a small glowing working-state card "
        "on top of a long archive of event logs, clean flat vector style",
        "out/smoke_bfl.jpg", width=1024, height=1024,
    )
    print(f"saved {path} in {time.monotonic() - t:.1f}s")
