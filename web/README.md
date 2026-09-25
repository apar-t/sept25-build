# Lane C: dashboard. Owner: see AGENTS.md "Lanes".

Reads nw_state_cards, nw_alerts, nw_ticks (latest state card per vendor = `argMax(card_json, updated_at)`).
Shows: supply-chain graph (company -> vendors -> sub-processors, red when status=red),
alert panel (before/after), tokens-per-tick chart (nights_watch vs naive).
Until real data flows, build against fixtures/snapshots and sample alerts.
