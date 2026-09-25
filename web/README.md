# Lane C: dashboard. Owner: see AGENTS.md "Lanes".

Reads nw_state_cards, nw_alerts, nw_ticks (latest state card per vendor = `argMax(card_json, updated_at)`).
Shows: supply-chain graph (company -> vendors -> sub-processors, red when status=red),
alert panel (before/after), tokens-per-tick chart (nights_watch vs naive).
Until real data flows, build against fixtures/snapshots and sample alerts.

## Landing page
`index.html` is the landing page: one static file, no build step. `drafts/` holds two alternative designs kept for reference.
Preview: `python3 -m http.server 8780 --directory web`, then open http://localhost:8780 (8765 is taken by `site/serve.py`).
Live at https://nightswatch.app (Cloudflare Worker in `site-worker/` serving the GitHub Pages copy; www redirects to it; domain registered at Cloudflare). Pages copy: https://vroy2008.github.io/nights-watch/ (GitHub Pages on the public repo `vroy2008/nights-watch`, which holds only `index.html`; this repo stays private). To publish your latest `web/index.html` (needs push access to that repo):
```bash
d=$(mktemp -d) && git clone -q https://github.com/vroy2008/nights-watch.git "$d" && cp web/index.html "$d/" && git -C "$d" commit -qam "Update from sept25-build $(git rev-parse --short HEAD)" && git -C "$d" push -q
```

## Contact form
The form posts to a Cloudflare Worker (`contact-worker/`), which commits each submission as `contacts/<date>/<time>-<id>.json` to the private repo `vroy2008/nights-watch-contacts`. The Worker holds `GITHUB_TOKEN`, a fine-grained token with Contents read/write on that one repo only; the page never sees it. It only accepts requests from nightswatch.app, the Pages site and localhost:8780, and drops bots that fill the hidden `website` field.
Deploy: `cd web/contact-worker && npx wrangler deploy`, then set `CONTACT_URL` in `index.html` to the printed `workers.dev` URL. Set the token once with `npx wrangler secret put GITHUB_TOKEN`.
