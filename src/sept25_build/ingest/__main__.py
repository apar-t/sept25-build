"""uv run python -m sept25_build.ingest [vendor ...]   read vendors from the demo site, save to nw_snapshots"""

import sys

from . import run_once

try:
    run_once(sys.argv[1:] or None)
except (ValueError, RuntimeError) as e:
    sys.exit(str(e))
