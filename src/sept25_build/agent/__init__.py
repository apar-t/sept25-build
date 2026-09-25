"""Lane B: the agent. Owner: apar-t.

Reads Snapshots, rewrites one StateCard per vendor, emits Alerts and TickLogs.
Interface the other lanes rely on:
    step(card: StateCard | None, snap: Snapshot, tick: int) -> (StateCard, list[Alert], TickLog)
    run_tick() -> list[Alert]         # latest snapshot per vendor -> nw_state_cards / nw_alerts / nw_ticks
"""

from ..contracts import Alert, TickLog
from .core import naive_tokens, step
from .store import MemoryStore, RawTreeStore


def run_tick(store=None, run_id: str = "live") -> list[Alert]:
    """Process the latest snapshot of every vendor that has something new. Returns this tick's alerts."""
    store = store or RawTreeStore()
    cards = store.latest_cards()
    tick = max((c.tick for c in cards.values()), default=0) + 1
    out_cards, out_alerts, out_ticks = [], [], []
    for snap in store.latest_snapshots():
        card = cards.get(snap.vendor)
        if card and card.last_snapshot_id == snap.snapshot_id:
            continue  # nothing new for this vendor since last tick
        try:
            new, alerts, log = step(card, snap, tick, run_id)
        except Exception as e:  # one bad vendor must not stop the others
            print(f"agent: skipped {snap.vendor} this tick: {type(e).__name__}: {e}")
            continue
        naive = TickLog(run_id=run_id, tick=tick, agent="naive", vendor=snap.vendor,
                        input_tokens=naive_tokens(store.last_naive_total(snap.vendor), snap, card is None))
        out_cards.append(new)
        out_alerts += alerts
        out_ticks += [log, naive]
    store.write(out_cards, out_alerts, out_ticks)
    return out_alerts
