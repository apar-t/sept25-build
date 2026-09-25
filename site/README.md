# Night's Watch Inc. Trust Center + vendor demo mirrors

The demo's baseline and its injection target. Night's Watch Inc. is a fictional company whose vendors are the hackathon sponsors. The site serves the company's own trust pages plus a **demo mirror** of each vendor's sub-processor list and data-processing terms. Every mirror page carries a "Demo mirror ... not affiliated with <Vendor>" banner. The content is illustrative and is not any vendor's actual terms.

Stdlib only. Pages are rendered from `site/live/*.json` on every request with `Cache-Control: no-store`, so an injection shows up on the very next fetch. `site/live/` is gitignored and is created from `site/seed/` on first start.

## Run

```bash
uv run python site/serve.py            # http://127.0.0.1:8765  (--port, --host)
bash site/tunnel.sh                    # starts serve.py if needed + Cloudflare quick tunnel
cat site/live/tunnel_url.txt           # the public https://<random>.trycloudflare.com base URL
```

The quick-tunnel URL changes every time `tunnel.sh` starts, so send lane A the new one. Ctrl-C stops the tunnel, and also the server if `tunnel.sh` started it.

## URLs (lane A fetches these)

Slugs: `aws`, `nimble`, `tinybird`, `liquid`, `bfl`.

| URL | What |
|---|---|
| `<base>/vendors/<slug>/subprocessors` | HTML table: Name / Purpose / Location / Website (Location "—" when no country; Website blank when no url) |
| `<base>/vendors/<slug>/subprocessors.json` | `[{"name","purpose","country","url"}]` (`"country": ""` when not listed, `"url": ""` when absent) |
| `<base>/vendors/<slug>/dpa` | data-processing terms, HTML |
| `<base>/vendors/<slug>/dpa.md` | the same terms as raw markdown (paragraphs separated by blank lines) |
| `<base>/vendors/<slug>/` | index for one vendor's mirror |
| `<base>/`, `/privacy`, `/terms`, `/subprocessors` | Night's Watch Inc.'s own trust pages (5 vendors, linked to their mirrors) |
| `<base>/companies/<slug>` | fictional third-party company profile: About, "Headquarters: <city>, <country>", "Data processing locations: <country>" (seed: `dataharvest`) |
| `<base>/companies/<slug>.json` | the same data: `{"slug","name","about","headquarters":{"city","country"},"data_processing_locations":[...]}` |

A sub-processor `url` stored as a site path (`/companies/dataharvest`) is served as an absolute URL built from the request's `Host` and `X-Forwarded-Proto`. cloudflared passes the public hostname and `https`, so fetched through the tunnel, `subprocessors.json` carries `https://<random>.trycloudflare.com/companies/dataharvest`, a link Nimble can fetch. Fetched via `127.0.0.1`, it carries a localhost URL instead.

The tinybird, nimble, liquid and bfl mirrors list "Amazon Web Services, Inc." (Cloud hosting, United States), which links them to the AWS node (fourth party). The seed state is all green: every country is approved, there's no training on customer data, and retention is 30 days.

## Inject (edits `site/live/`, prints what changed)

```bash
# default demo move -> R1 (Singapore is not an approved country)
uv run python site/inject.py add-subprocessor --vendor tinybird --name "DataHarvest Ltd" --country Singapore

# harder demo move -> no country listed; the location is only on the sub-processor's website
uv run python site/inject.py add-subprocessor --vendor tinybird --name "DataHarvest Ltd" --country "" --url /companies/dataharvest

uv run python site/inject.py remove-subprocessor --vendor tinybird --name "DataHarvest Ltd"
uv run python site/inject.py change-policy --vendor tinybird --clause training    # reword in place -> R2
uv run python site/inject.py change-policy --vendor tinybird --clause retention   # 24 months -> R3
uv run python site/inject.py append-clause --vendor tinybird --clause training    # add a new paragraph, keep the original
uv run python site/inject.py touch --vendor tinybird                              # noise: "Last updated:" = today
uv run python site/inject.py reset [--vendor tinybird]                            # back to seed (full reset also restores companies.json)
uv run python site/inject.py status                                               # one line per vendor; no-country entries show as "location unknown"
```

### The "follow the link" move

`--country "" --url /companies/dataharvest` adds DataHarvest Ltd to Tinybird's list with an empty Location and a Website link. The rules can't decide R1 (approved countries) without a country, so the diff alone is inconclusive. The agent follows the sub-processor's `url` with Nimble, reads the company page ("Headquarters: Singapore, Singapore"; "Data processing locations: Singapore") and finds Singapore, which is not an approved country, so R1 fires. Undo it with `remove-subprocessor` or `reset`.
