"""Night's Watch as a long-running agent: tick forever, survive crashes, never double-alert.

    uv run python -m sept25_build.agent.runner                  # tick every 30s against RawTree
    uv run python -m sept25_build.agent.runner --fetch          # also run lane A's ingest each cycle
    uv run python -m sept25_build.agent.runner --fetch --vendors tinybird --agent --narrate --cycles 1   # stage tick
    uv run python -m sept25_build.agent.runner --store file:/tmp/rehearsal --fetch --agent   # rehearsal: no live writes
    uv run python -m sept25_build.agent.runner --every 10 --cycles 3 --adaptive
    uv run python -m sept25_build.agent.runner --fetch --agent       # + the tool-using agent (GPT-5.6 Sol)
    uv run python -m sept25_build.agent.runner --store file:.nw_state   # durable local store instead
    uv run python -m sept25_build.agent.runner --narrate                # print each decision (demo)

What makes it long-horizon-safe:
  - Memory lives in the store, not the process. On start it RECOVERS: loads every vendor's latest
    committed state card, reports what it remembers and what it still has to process, and
    rebuilds any vendor whose card is missing or corrupt by replaying that vendor's snapshot log.
  - Writes are idempotent and cards are the commit point (see agent.run_tick), so killing the
    process at any moment loses nothing and duplicates nothing: the next start resumes the tick.
  - A lease with a heartbeat keeps a single runner per store (three teammates, one agent).
  - Per-cycle errors back off and retry instead of killing the loop; SIGINT/SIGTERM finish the
    current cycle, then release the lease.
"""

import argparse
import getpass
import json
import signal
import socket
import time
from datetime import datetime, timezone
from pathlib import Path

from .. import rawtree
from ..contracts import TABLES, now
from . import MemoryStore, RawTreeStore, llm, narrate, run_tick
from .core import memory_ops, step

LEASE_SECS = 90


class Lease:
    """Single-runner lock. RawTree is append-only, so the lease is the latest heartbeat row."""

    def __init__(self, store_kind: str, holder: str, run_id: str, path: Path | None = None):
        self.kind, self.holder, self.run_id, self.path = store_kind, holder, run_id, path

    def current(self) -> dict | None:
        if self.kind == "file":
            return json.loads(self.path.read_text()) if self.path.exists() else None
        try:
            rows = rawtree.query(f"SELECT toString(holder) AS holder, toString(run_id) AS run_id, "
                                 f"toString(state) AS state, toString(heartbeat_at) AS at FROM {TABLES['lease']}")
        except RuntimeError:
            return None
        return max(rows, key=lambda r: r["at"]) if rows else None

    def _put(self, state: str) -> None:
        row = {"holder": self.holder, "run_id": self.run_id, "state": state, "heartbeat_at": now().isoformat()}
        if self.kind == "file":
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(row))
        else:
            rawtree.insert("lease", [row])

    def acquire(self, takeover: bool) -> None:
        cur = self.current()
        if cur and cur["state"] == "running" and cur["holder"] != self.holder:
            at = datetime.fromisoformat(cur["at"].replace(" ", "T"))
            age = (now() - (at if at.tzinfo else at.replace(tzinfo=timezone.utc))).total_seconds()
            if age < LEASE_SECS and not takeover:
                raise SystemExit(f"another runner holds the lease: {cur['holder']} (heartbeat {age:.0f}s ago). "
                                 f"Stop it, wait {LEASE_SECS - age:.0f}s, or pass --takeover.")
            print(f"lease: taking over from {cur['holder']} (last heartbeat {age:.0f}s ago)")
        self._put("running")

    def heartbeat(self) -> None:
        self._put("running")

    def release(self) -> None:
        self._put("stopped")


def make_store(spec: str):
    if spec == "rawtree":
        return RawTreeStore(), "rawtree", None
    if spec.startswith("file:"):
        from .filestore import FileStore  # built alongside; durable JSONL store
        root = Path(spec[5:])
        return FileStore(root), "file", root / "lease.json"
    if spec == "memory":
        return MemoryStore(), "file", Path(".nw_state/lease-memory.json")
    raise SystemExit(f"unknown --store {spec!r}")


def recover(store, run_id: str) -> None:
    """Memory recovery on start: report what the agent remembers, rebuild what it lost."""
    cards = store.latest_cards()
    snaps = {s.vendor: s for s in store.latest_snapshots()}
    print(f"recovery: {len(cards)} state cards loaded from the store")
    for v, c in sorted(cards.items()):
        pending = v in snaps and snaps[v].snapshot_id != c.last_snapshot_id
        print(f"  {v:16} tick {c.tick:<4} {c.status:5} findings={[f.rule for f in c.open_findings]} "
              f"watched={c.counters.get('ticks', 0)} discarded={c.counters.get('noise', 0) + c.counters.get('unchanged', 0)}"
              + ("  <- has an unprocessed snapshot, resuming" if pending else ""))
    lost = [v for v in snaps if v not in cards]
    for v in lost:
        history = store.snapshots_for(v) if hasattr(store, "snapshots_for") else []
        if len(history) <= 1:
            continue  # brand-new vendor: the next tick builds its baseline normally
        print(f"  {v:16} card missing or corrupt: rebuilding from {len(history)} logged snapshots")
        card, alerts_seen, base_tick = None, 0, max((c.tick for c in cards.values()), default=0)
        for i, snap in enumerate(history[:-1]):   # the newest one is left for the normal tick
            new, alerts, log = step(card, snap, base_tick + i + 1, run_id)
            card, alerts_seen = new, alerts_seen + len(alerts)
        store.write([card], [], [], memory_ops(None, card, card.tick, run_id))
        print(f"  {v:16} rebuilt: tick {card.tick}, {card.status}, ledger {len(card.ledger)} sub-processors, "
              f"{alerts_seen} historical alerts not re-sent")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--every", type=float, default=30, help="seconds between cycles")
    ap.add_argument("--cycles", type=int, default=0, help="stop after N cycles (0 = forever)")
    ap.add_argument("--fetch", action="store_true", help="run lane A's ingest.run_once() each cycle")
    ap.add_argument("--vendors", nargs="*", default=None,
                    help="with --fetch: only these vendors (e.g. tinybird) to save Nimble credits and time")
    ap.add_argument("--adaptive", action="store_true", help="skip quiet vendors until due (card.check_every)")
    ap.add_argument("--store", default="rawtree", help="rawtree | file:<dir> | memory")
    ap.add_argument("--run-id", default="live")
    ap.add_argument("--holder", default=f"{getpass.getuser()}@{socket.gethostname()}")
    ap.add_argument("--takeover", action="store_true")
    ap.add_argument("--no-llm", action="store_true")
    ap.add_argument("--agent", action="store_true", help="let the tool-using agent investigate open questions")
    ap.add_argument("--narrate", action="store_true", help="print what the agent does, step by step (demo)")
    args = ap.parse_args()
    narrate.enabled = args.narrate
    if args.no_llm:
        llm.disable()

    store, kind, lease_path = make_store(args.store)
    lease = Lease(kind, args.holder, args.run_id, lease_path)
    lease.acquire(args.takeover)
    print(f"runner: {args.holder} holds the lease; store={args.store} LLM={llm.describe()} reviewer={llm.REVIEW_MODEL}")
    stop = {"now": False}
    signal.signal(signal.SIGINT, lambda *_: stop.update(now=True))
    signal.signal(signal.SIGTERM, lambda *_: stop.update(now=True))

    recover(store, args.run_id)
    cycle, failures = 0, 0
    try:
        while not stop["now"] and (not args.cycles or cycle < args.cycles):
            cycle += 1
            t0 = time.monotonic()
            try:
                if args.fetch:
                    from .. import ingest
                    if kind != "rawtree" and hasattr(ingest, "fetch_vendor"):
                        # rehearsal store: read through Nimble but keep snapshots OUT of live RawTree
                        from ..contracts import VENDORS
                        snaps = [x for x in (ingest.fetch_vendor(v) for v in (args.vendors or list(VENDORS))) if x]
                        store.add_snapshots(snaps)
                    elif hasattr(ingest, "run_once"):
                        fresh = {x.snapshot_id for x in (ingest.run_once(args.vendors) or [])}
                        # RawTree has a short read-after-write lag: wait (<= 6s) until every snapshot we
                        # just inserted is visible, so this tick never misses it
                        deadline = time.monotonic() + 6
                        while fresh and time.monotonic() < deadline:
                            try:
                                seen = {x.snapshot_id for x in store.latest_snapshots()}
                            except Exception:  # noqa: BLE001 - a failed read just means "not yet"
                                seen = set()
                            if fresh <= seen:
                                break
                            time.sleep(0.5)
                    else:
                        print("runner: ingest.run_once() not available yet (lane A); agent only")
                alerts = run_tick(store, run_id=args.run_id, adaptive=args.adaptive, agent=args.agent)
                failures = 0
                stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
                print(f"[{stamp}] cycle {cycle}: {len(alerts)} alert(s) in {time.monotonic() - t0:.1f}s")
                for a in alerts:
                    print(f"    {'VIOLATION' if a.kind == 'violation' else 'resolved '} {a.rule}  {a.title}")
            except Exception as e:
                failures += 1
                print(f"runner: cycle {cycle} failed ({type(e).__name__}: {e}); memory is safe in the store, retrying")
            lease.heartbeat()
            wait = args.every if not failures else min(args.every * 2 ** failures, 300)
            while wait > 0 and not stop["now"] and (not args.cycles or cycle < args.cycles):
                time.sleep(min(1, wait))
                wait -= 1
    finally:
        lease.release()
        print("runner: stopped cleanly, lease released")


if __name__ == "__main__":
    main()
