# Sponsor tool setup

Checked 2026-09-25. Sources are the vendors' current docs.

## Liquid AI: LFM2.5-2.6B running locally
- Native tool calling, 128K context, OpenAI-compatible server. LEAP SDK is deprecated; there are no LFM2.5 models on Bedrock.
```bash
llama-server -hf LiquidAI/LFM2.5-2.6B-GGUF:Q4_K_M --jinja --port 8080 \
  -c 32768 -fa on -ngl 99 --temp 0.1 --top-k 50 --repeat-penalty 1.1
```
- Env: `LFM_BASE_URL=http://localhost:8080/v1`, `LFM_MODEL=LFM2.5-2.6B`
- Fallback: OpenRouter `liquid/lfm-2.5-2.6b:free`. Capped at 50 requests/day.
- Gotchas:
  - `--jinja` is required for tool calls.
  - The model always thinks first: strip `<think>…</think>` from replies and give it enough `max_tokens`.
  - Keep it to 3–8 tools; it sometimes describes a tool call instead of making it.
- Role: cheap compaction and state-update model. `response_format` with a json_schema forces valid JSON.

## Tinybird / RawTree
- **RawTree** (rawtree.com) is a Tinybird product: schema-free event database, SQL reads, MCP. Private beta, so ask the Tinybird people on site for an invite.
  ```bash
  curl -fsSL https://rawtree.com/install.sh | bash   # rtree
  rtree login && rtree database create agent
  ```
  - HTTP API: `POST https://api.rawtree.com/v1/tables/<t>` (JSON array), `POST /v1/query {"sql": ...}`
  - Headers: `Authorization: Bearer rt_…`, `x-rawtree-database`
  - No Python SDK; the query API is read-only SQL.
  - Verified 2026-09-25:
    - Account and key work.
    - Inserting creates the table automatically, and every column comes back as `Dynamic`.
    - Nested keys flatten into columns such as `payload.from`.
    - Full endpoint list: https://rawtree.com/openapi.json
- **Tinybird Forward** fallback: CLI `tb` 4.6.21 is installed. Needs `export PATH="$HOME/.local/bin:$PATH"`.
  ```bash
  tb login --host https://api.us-west-2.aws.tinybird.co
  tb init --type cli --folder .
  tb --cloud deploy
  ```
  - Ingest: `POST {host}/v0/events?name=<ds>&wait=true` (NDJSON)
  - Read an endpoint: `GET {host}/v0/pipes/<pipe>.json`
  - Ad-hoc SQL: `POST {host}/v0/sql`
  - MCP: `https://mcp.tinybird.co?token=<read token>`
- Gotchas:
  - Ignore Classic-era docs (`tb push`, `tb auth`).
  - Free plan: 1,000 requests/day, 10 s query timeout.
  - A token only works in its own region; the wrong host returns 403.
  - Rows that don't match the schema go silently to `<ds>_quarantine`.
- Role: append-only log of agent events is the record; the agent rebuilds `current_state` from it (`argMax(value, ts)` per key) instead of carrying history in context.

## Black Forest Labs: FLUX images (third tool)
- Account at dashboard.bfl.ai. **Add credits before the first call works** ($10–20 suggested; 1 credit = $0.01). The API key is shown only once.
- Client: `src/sept25_build/bfl.py`, with `credits()` and `generate(prompt, out, model=..., **params)`.
- Test: `uv run scripts/smoke_bfl.py` checks the key for free; `--generate` makes one image (~$0.03).
- Models:
  - `flux-2-pro-preview`: default. Its output shifts as the preview updates; use `flux-2-pro` for reproducible output.
  - `flux-2-klein-*`: cheapest and fastest.
  - `flux-3-video`: video with audio, $0.17–0.80 per second.
- Edits: pass `input_image` (and optionally `input_image_2`, and so on) to `generate`, which calls the same endpoint.
- MCP: `claude mcp add --transport http FLUX https://mcp.bfl.ai` (OAuth sign-in).
- Gotchas:
  - Result URLs expire after 10 minutes; `generate` downloads immediately.
  - A bad key returns 422, not 401.
  - A 402 means out of credits.
  - Moderation can reject a prompt.

## Nimble: web data (Night's Watch "observe" step)
- `nimble_python` (installed), key env var `NIMBLE_API_KEY`. Free trial: 5,000 pages, no card.
```python
from nimble_python import Nimble
n = Nimble(api_key=os.environ["NIMBLE_API_KEY"])
n.search(query=..., max_results=5, search_depth="lite", time_range="week")
n.extract.run(url=..., formats=["markdown"]).data.markdown
```
- Gotchas:
  - v2 API since July 2026; older tutorials are stale.
  - `agents.run` defaults to high effort ($0.50 per task).
  - Use the SDK rather than MCP, so raw pages stay out of the agent's context.
- Role: the agent's "observe" step for a changing outside world.

## Hackathon
- No resource page or credits found. Ask at kickoff.
- Example of the same pattern from another team: github.com/somul18/LongHorizonAgent_v1
