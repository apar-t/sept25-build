# Lane C: dashboard. Owner: see AGENTS.md "Lanes".

Reads nw_state_cards, nw_alerts, nw_ticks (latest state card per vendor = `argMax(card_json, updated_at)`).
Shows: supply-chain graph (company -> vendors -> sub-processors, red when status=red),
alert panel (before/after), tokens-per-tick chart (nights_watch vs naive).
Until real data flows, build against fixtures/snapshots and sample alerts.

## Landing page
`index.html` is the landing page: one static file, no build step. `drafts/` holds two alternative designs kept for reference.
Preview: `python3 -m http.server 8780 --directory web`, then open http://localhost:8780 (8765 is taken by `site/serve.py`).
The contact form has no backend yet.

Public copy: https://vroy2008.github.io/nights-watch/ (GitHub Pages on the public repo `vroy2008/nights-watch`, which holds only `index.html`; this repo stays private). To publish your latest `web/index.html` (needs push access to that repo):
```bash
d=$(mktemp -d) && git clone -q https://github.com/vroy2008/nights-watch.git "$d" && cp web/index.html "$d/" && git -C "$d" commit -qam "Update from sept25-build $(git rev-parse --short HEAD)" && git -C "$d" push -q
```
