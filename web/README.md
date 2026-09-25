# Lane C: dashboard. Owner: see AGENTS.md "Lanes".

Reads nw_state_cards, nw_alerts, nw_ticks (latest state card per vendor = `argMax(card_json, updated_at)`).
Shows: supply-chain graph (company -> vendors -> sub-processors, red when status=red),
alert panel (before/after), tokens-per-tick chart (nights_watch vs naive).
Until real data flows, build against fixtures/snapshots and sample alerts.

## Landing page
`index.html` is the landing page: one static file, no build step. `drafts/` holds two alternative designs kept for reference.
Preview: `python3 -m http.server 8780 --directory web`, then open http://localhost:8780 (8765 is taken by `site/serve.py`).
The contact form has no backend yet.
