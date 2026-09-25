"""Lane B: the agent. Owner: apar-t.

Reads Snapshots, rewrites one StateCard per vendor, emits Alerts and TickLogs.
Interface the other lanes rely on:
    step(card: StateCard | None, snap: Snapshot, tick: int) -> (StateCard, list[Alert], TickLog)
    run_tick() -> list[Alert]         # latest snapshot per vendor -> nw_state_cards / nw_alerts / nw_ticks
"""
