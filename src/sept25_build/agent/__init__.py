"""Lane B: the agent. Owner: apar-t.

Reads Snapshots, rewrites one StateCard per vendor, emits Alerts and TickLogs.
Interface the other lanes rely on:
    step(card: StateCard | None, snap: Snapshot, tick: int) -> (StateCard, list[Alert], TickLog)
    run_tick() -> list[Alert]         # latest snapshot per vendor -> nw_state_cards / nw_alerts / nw_ticks
"""

from ..contracts import Alert, TickLog
from . import graph
from .core import memory_ops, naive_tokens, step
from .store import MemoryStore, RawTreeStore


def run_tick(store=None, run_id: str = "live", adaptive: bool = False, agent: bool = False) -> list[Alert]:
    """Process the latest snapshot of every vendor that has something new. Returns this tick's alerts.

    Crash-safe: the store writes ops, alerts and ticks first and the cards last, and every row has
    an idempotency key. A tick that died before its cards were written is replayed with the same
    tick number, so it produces the same keys and nothing is duplicated or lost.
    adaptive=True skips vendors whose card says they're quiet (check_every) until they're due.
    agent=True lets the tool-using agent (episode.py) investigate open questions and changes.
    """
    store = store or RawTreeStore()
    cards = store.latest_cards()
    tick = max((c.tick for c in cards.values()), default=0) + 1
    out_cards, out_alerts, out_ticks, out_ops, episodes = [], [], [], [], []
    for snap in store.latest_snapshots():
        card = cards.get(snap.vendor)
        if card and card.last_snapshot_id == snap.snapshot_id:
            continue  # nothing new for this vendor since last tick
        if adaptive and card and tick - card.tick < card.check_every:
            continue  # quiet vendor, not due yet: its snapshot waits for the next due tick
        try:
            new, alerts, log = step(card, snap, tick, run_id, agent=agent, episodes=episodes, store=store)
            ops = memory_ops(card, new, tick, run_id)
        except Exception as e:  # one bad vendor must not stop the others
            print(f"agent: skipped {snap.vendor} this tick: {type(e).__name__}: {e}")
            continue
        naive = TickLog(run_id=run_id, tick=tick, agent="naive", vendor=snap.vendor,
                        input_tokens=naive_tokens(store.last_naive_total(snap.vendor, before_tick=tick), snap, card is None))
        out_cards.append(new)
        out_alerts += alerts
        out_ticks += [log, naive]
        out_ops += ops
    # Fourth-party links: an upstream vendor turning red changes cards that didn't change themselves.
    merged = {**cards, **{c.vendor: c for c in out_cards}}
    for c in graph.link(merged):
        if c not in out_cards:
            out_cards.append(c)
    if episodes and hasattr(store, "write_episodes"):
        store.write_episodes(episodes)  # before cards: a replayed tick re-runs the episode, id dedups the trace
    store.write(out_cards, out_alerts, out_ticks, out_ops)
    return out_alerts
