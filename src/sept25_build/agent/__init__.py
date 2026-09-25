"""Lane B: the agent. Owner: apar-t.

Reads Snapshots, rewrites one StateCard per vendor, emits Alerts and TickLogs.
Interface the other lanes rely on:
    step(card: StateCard | None, snap: Snapshot, tick: int) -> (StateCard, list[Alert], TickLog)
    run_tick() -> list[Alert]         # latest snapshot per vendor -> nw_state_cards / nw_alerts / nw_ticks
"""

from ..contracts import Alert, TickLog
from . import graph, narrate
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
    tally = {"vendors": 0, "discarded": 0, "material": 0, "not_due": 0, "failed": 0, "liquid": 0, "review": 0}
    narrate.say(f"tick {tick}: {len(cards)} state cards in memory, reading latest snapshots")
    for snap in store.latest_snapshots():
        card = cards.get(snap.vendor)
        tally["vendors"] += 1
        if card and card.last_snapshot_id == snap.snapshot_id:
            tally["discarded"] += 1
            narrate.say(f"{snap.vendor}: skip, unchanged since {narrate.clock(snap.fetched_at)}")
            continue  # nothing new for this vendor since last tick
        if adaptive and card and tick - card.tick < card.check_every:
            tally["not_due"] += 1
            narrate.say(f"{snap.vendor}: skip, not due (adaptive): every {card.check_every} ticks, last t{card.tick}")
            continue  # quiet vendor, not due yet: its snapshot waits for the next due tick
        narrate.say(f"{snap.vendor}: new snapshot fetched {narrate.clock(snap.fetched_at)}")
        try:
            new, alerts, log = step(card, snap, tick, run_id, agent=agent, episodes=episodes, store=store)
            ops = memory_ops(card, new, tick, run_id)
        except Exception as e:  # one bad vendor must not stop the others
            tally["failed"] += 1
            print(f"agent: skipped {snap.vendor} this tick: {type(e).__name__}: {e}")
            continue
        naive = TickLog(run_id=run_id, tick=tick, agent="naive", vendor=snap.vendor,
                        input_tokens=naive_tokens(store.last_naive_total(snap.vendor, before_tick=tick), snap, card is None))
        if narrate.enabled:
            _narrate_step(card, new, log, naive, ops, tally)
        out_cards.append(new)
        out_alerts += alerts
        out_ticks += [log, naive]
        out_ops += ops
    # Fourth-party links: an upstream vendor turning red changes cards that didn't change themselves.
    merged = {**cards, **{c.vendor: c for c in out_cards}}
    for c in graph.link(merged):
        if c not in out_cards:
            out_cards.append(c)
            narrate.say(f"{c.vendor}: fourth-party link, card updated from an upstream vendor (now {c.status})")
    if episodes and hasattr(store, "write_episodes"):
        store.write_episodes(episodes)  # before cards: a replayed tick re-runs the episode, id dedups the trace
    store.write(out_cards, out_alerts, out_ticks, out_ops)
    if narrate.enabled:
        t = tally
        narrate.say(f"tick {tick} done, {t['vendors']} vendors: {t['discarded']} discarded, {t['material']} material"
                    + (f", {t['not_due']} not due" if t["not_due"] else "")
                    + (f", {t['failed']} failed" if t["failed"] else "")
                    + f" | Liquid {t['liquid']} sentences | reviewer {t['review']} calls | episodes {len(episodes)}"
                    + f" | {len(out_alerts)} alert(s)")
    return out_alerts


def _narrate_step(card, new, log, naive, ops, tally) -> None:
    """Print-only: what this step decided, what it wrote to memory, and what it cost. Never raises."""
    try:
        prev = card.counters if card else {}
        if log.material:
            kind = "material"
        else:
            kind = "noise" if new.counters.get("noise", 0) > prev.get("noise", 0) else "unchanged"
        old_sv = card.sentence_verdicts if card else {}
        tally["liquid"] += sum(1 for k, v in new.sentence_verdicts.items() if k not in old_sv and
                               "liquid" in (v.get("by"), (v.get("first_opinion") or {}).get("by")))
        tally["review"] += log.review_calls
        if kind == "material":
            tally["material"] += 1
            was = card.status if card else "new"
            narrate.say(f"  material -> card rewritten, status " + (f"stays {was}" if was == new.status else f"{was} -> {new.status}"))
            narrate.say(f"  memory: {narrate.ops_line(ops)}")
        else:
            tally["discarded"] += 1
            narrate.say(f"  {kind} -> discarded, counter {kind} {prev.get(kind, 0)} -> {new.counters.get(kind, 0)}, "
                        + (f"memory: {narrate.ops_line(ops)}" if ops else "nothing written to memory"))
        narrate.say(f"  context: card {log.card_tokens} tok | naive {naive.input_tokens} tok (computed, not run)")
    except Exception as e:  # noqa: BLE001
        narrate.say(f"  (narration error: {type(e).__name__}: {e})")
