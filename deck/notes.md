# Night's Watch demo: speaker notes (3:00 total)

| When | On screen | Say (one line) |
|---|---|---|
| 0:00-0:10 | 1 Cover | Night's Watch watches the vendors your data goes to, and their vendors, for months. |
| 0:10-0:30 | 2 Vendors have vendors | A vendor adds a sub-processor in a new country. The only notice is a changed web page. |
| 0:30-0:40 | 3 Live demo | Night's Watch Inc., five vendors, three rules. We change the Tinybird demo mirror three times. |
| 0:40-2:15 | **Live prototype** | Step 1 `touch`: dropped, 0 alerts. Step 2 add DataHarvest Ltd: Nimble fetch, agent finds the country, R1, node red. Step 3 training clause: R2. Commands: `uv run python -m sept25_build.agent.preflight` prints the cue card. |
| 2:15-2:40 | 4 How it works | Each run starts from a small state card plus the change. No chat history. History stays in RawTree. |
| 2:40-3:00 | 5 A year, simulated | Over 365 days the context stays near 1.8k tokens. Full history would pass Liquid's window by day 56. 5 of 5 alerts. |

## If asked

- **Is the naive line a real run?** No. It is computed with the same token estimate (len/4), not run. `uv run python -m sept25_build.agent.proof`.
- **Is the year real?** Simulated: 6 vendors, 365 days, seed 7, rules-only path (no LLM calls in the sim). The live demo uses the real pipeline.
- **What's the second model?** A reviewer (default `openai/gpt-5.6-sol` via OpenRouter) gives a second opinion only on sentences that would change a verdict. It also runs the tool-using agent episodes.
- **Why a mirror?** We can't edit real vendor pages. The injected vendor is always labeled "demo mirror".
- **Crash test:** 60 days, 8 crashes at 5 different write points. Alerts, ticks and memory ops match a run that never crashed, final cards identical.
- **Ledger:** when DataHarvest is re-added after removal, the card's ledger turns the alert into "re-added, 2nd time, escalate". Costs 136 tokens, kept out of the prompt.

## Don't say

- That AWS Bedrock is in the pipeline. The current code on main doesn't call it.
- That the vendors in the demo made these changes. The changes are on our mirror.
