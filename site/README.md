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
| `<base>/vendors/<slug>/subprocessors` | HTML table: Name / Purpose / Location |
| `<base>/vendors/<slug>/subprocessors.json` | `[{"name","purpose","country"}]` |
| `<base>/vendors/<slug>/dpa` | data-processing terms, HTML |
| `<base>/vendors/<slug>/dpa.md` | the same terms as raw markdown (paragraphs separated by blank lines) |
| `<base>/vendors/<slug>/` | index for one vendor's mirror |
| `<base>/`, `/privacy`, `/terms`, `/subprocessors` | Night's Watch Inc.'s own trust pages (5 vendors, linked to their mirrors) |

The tinybird, nimble, liquid and bfl mirrors list "Amazon Web Services, Inc." (Cloud hosting, United States), which links them to the AWS node (fourth party). The seed state is all green: every country is approved, there's no training on customer data, and retention is 30 days.

## Inject (edits `site/live/`, prints what changed)

```bash
# default demo move -> R1 (Singapore is not an approved country)
uv run python site/inject.py add-subprocessor --vendor tinybird --name "DataHarvest Ltd" --country Singapore

uv run python site/inject.py remove-subprocessor --vendor tinybird --name "DataHarvest Ltd"
uv run python site/inject.py change-policy --vendor tinybird --clause training    # reword in place -> R2
uv run python site/inject.py change-policy --vendor tinybird --clause retention   # 24 months -> R3
uv run python site/inject.py append-clause --vendor tinybird --clause training    # add a new paragraph, keep the original
uv run python site/inject.py touch --vendor tinybird                              # noise: "Last updated:" = today
uv run python site/inject.py reset [--vendor tinybird]                            # back to seed
uv run python site/inject.py status                                               # one line per vendor
```
