# Lane C: dashboard. Owner: see AGENTS.md "Lanes".

Reads nw_state_cards, nw_alerts, nw_ticks (latest state card per vendor = `argMax(card_json, updated_at)`).
Shows: supply-chain graph (company -> vendors -> sub-processors, red when status=red),
alert panel (before/after), tokens-per-tick chart (nights_watch vs naive).
Until real data flows, build against fixtures/snapshots and sample alerts.

## Landing page
`index.html` is the landing page: one static file, no build step. `drafts/` holds two alternative designs kept for reference.
Preview: `python3 -m http.server 8780 --directory web`, then open http://localhost:8780 (8765 is taken by `site/serve.py`).
Live at https://nightswatch.app (Cloudflare Worker in `site-worker/` serving the GitHub Pages copy; www redirects to it; domain registered at Cloudflare). Pages copy: https://vroy2008.github.io/nights-watch/ (GitHub Pages on the public repo `vroy2008/nights-watch`, which holds only `index.html`; this repo stays private). To publish your latest `web/index.html` and the demo (needs push access to that repo):
```bash
d=$(mktemp -d) && git clone -q https://github.com/vroy2008/nights-watch.git "$d" && cp web/index.html web/demo.html web/guided.html web/demo.json "$d/" && git -C "$d" add -A && git -C "$d" commit -qam "Update from sept25-build $(git rev-parse --short HEAD)" && git -C "$d" push -q
```

## Contact form
The form posts to a Cloudflare Worker (`contact-worker/`), which commits each submission as `contacts/<date>/<time>-<id>.json` to the private repo `vroy2008/nights-watch-contacts`. The Worker holds `GITHUB_TOKEN`, a fine-grained token with Contents read/write on that one repo only; the page never sees it. It only accepts requests from nightswatch.app, the Pages site and localhost:8780, and drops bots that fill the hidden `website` field.
Deploy: `cd web/contact-worker && npx wrangler deploy`, then set `CONTACT_URL` in `index.html` to the printed `workers.dev` URL. Set the token once with `npx wrangler secret put GITHUB_TOKEN`.

## Public demo
Two static replays of one real recorded run (`demo.json`), so they need no server and no keys:
- `demo.html` (nightswatch.app/demo.html): lane B's app page (`src/sept25_build/agent/ui/app.html`), built by `python3 web/build_app_demo.py`. Connect, setup, watching, then simulate: add sub-processor, then AI-training clause, then undo (the order the run recorded).
- `guided.html`: the four-step guided console (`console.html`), built by `python3 web/build_demo.py`.
Both builders fail loudly if an anchor they edit moved. Regenerate the recording from a rehearsal run with `NW_TABLE_SUFFIX=_e2e uv run python web/export_demo.py` (read-only; checks #2-#6), rebuild, then publish.

## Live company checks
On `demo.html`, our demo company replays the recording; any other website is checked live:
page -> `POST /check` on the contact Worker (`contact-worker/`) -> GitHub Actions in the private repo `vroy2008/nights-watch-live` runs lane B's check (`live/run_check.py`, Nimble only) -> reports progress and the result to `POST /check/<id>/report` -> D1 database `nights-watch-live` (`contact-worker/schema.sql`) -> page polls `GET /check/<id>`.
- The same site checked again within 24 h reuses the stored result. At most 20 new checks per hour. A check takes about 15-60 s.
- Secrets: the Worker's `GITHUB_TOKEN` needs Actions read/write on `vroy2008/nights-watch-live` (plus Contents on the contacts repo); `REPORT_SECRET` is shared by the Worker and that repo; `NIMBLE_API_KEY` is a secret on that repo.
- After lane B changes the check, run `bash web/live/sync.sh` to copy the code into that repo.
- Results: `npx wrangler d1 execute nights-watch-live --remote --command "SELECT host, status, datetime(created_at/1000,'unixepoch') FROM checks ORDER BY created_at DESC LIMIT 20"` (from `web/contact-worker`).

