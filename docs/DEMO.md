# Night's Watch: stage runbook and talk track

The stage console (`uv run python -m sept25_build.agent.console`, http://127.0.0.1:8790) has these buttons: **Noise, Inject DataHarvest (no country), Inject training clause, Reset, Run tick, Proof scorecard**. Its panels are the supply-chain graph, alerts, agent episode trace, memory edits and the token chart. The numbers below come from the `proof` and `preflight` runs at 14:57 PDT, 2026-09-25.

## 1. T-30 min setup checklist

- [ ] **Present from this laptop.** The keys, `.env`, the site server (:8765) and the tunnel all live here.
- [ ] **Tunnel:** keep the tunnel that is already running (preflight showed it PASS), or run `bash site/tunnel.sh` early. Public DNS takes about 40-60 s, and Nimble's first fetch of a new tunnel can take about 40 s. A new URL means you must tell lane A.
- [ ] `uv run python -m sept25_build.agent.preflight --warm` must show **0 FAIL**. At 14:57 it showed 10 PASS, 0 WARN, 0 FAIL.
- [ ] `uv run python site/inject.py reset`, then `uv run python site/inject.py status`: all five vendors should be green.
- [ ] Start the console with `uv run python -m sept25_build.agent.console` and open http://127.0.0.1:8790.
- [ ] Opener: `python3 -m http.server 8780 --directory web` and open http://127.0.0.1:8780 (the landing page).
- [ ] `uv run python -m sept25_build.agent.proof` (offline, about 10 s) refreshes `out/proof.json`.
- [ ] **Never inject against the stage tables before going live.** Rehearse with `NW_TABLE_SUFFIX=_e2e` (tables already exist) on the tick/console: `NW_TABLE_SUFFIX=_e2e uv run python -m sept25_build.agent.console`. The suffix only separates memory: injections always edit the one shared `site/live/`, so **`inject.py reset` after every rehearsal**. A brand-new suffix needs one baseline tick, run twice (the first insert creates the tables and RawTree takes a moment before they're queryable).
- [ ] Keep a terminal open on the repo root with the cue card from section 3 ready to paste.

## 2. Three-minute talk track

| Time | Do | Audience sees | Say |
|---|---|---|---|
| 0:00-0:20 | Landing page | Night radar hero | "Your vendors hand your data to their own sub-processors. GDPR Art. 28 makes that your problem, and the only notice you get is a quietly edited web page. That's compliance drift." |
| 0:20-0:50 | Switch to the console: graph + token chart | Night's Watch Inc. → 5 vendors → fourth parties, all green; a flat line against a climbing one | "It's a long-horizon agent. Each vendor has a small state card that gets **rewritten, never appended**. Memory is per sentence, so it only reads sentences it hasn't seen. A ledger remembers every sub-processor that ever came and went. Every memory edit goes in a journal. Each investigation runs as a fresh-context agent episode. The full history stays in RawTree and is recalled on demand, never stuffed into the prompt." |
| 0:50-1:10 | **Noise** → **Run tick** | 0 alerts, tick marked discarded, **0 LLM calls**, token line stays flat | "Tinybird's mirror bumped its 'Last updated' date. That's noise, so it's discarded with no model call." |
| 1:10-1:45 | **Inject DataHarvest (no country)** → **Run tick** | Episode trace: the agent fetches DataHarvest's page **via Nimble** and reads "Headquarters: Singapore". The Tinybird node turns **red**. Alert: `VIOLATION R1 Tinybird (demo mirror) added DataHarvest Ltd (Singapore, found by the agent)` | "The new sub-processor lists no country. Rules alone can't decide, so the agent follows the link with Nimble, finds Singapore, which isn't an approved country, and flags R1." |
| 1:45-2:15 | **Inject training clause** → **Run tick** | Memory edits: open_finding. Alert: `VIOLATION R2 Tinybird (demo mirror) now allows training on customer data`, explanation says "confirmed by gpt-5.6-sol" | "One sentence of the DPA changed. Liquid judges every new sentence and flags this one. GPT-5.6 Sol gives a second opinion and confirms it. That's R2: our data used for training." |
| 2:15-2:30 | **Reset** → **Run tick** | Both findings close: `resolved R1 ... no longer sends data to DataHarvest Ltd in Singapore`, `resolved R2 ... no longer allows training on customer data`. Node back to green | "The vendor backed out. It resolves itself, and the ledger remembers, so a second add would say 're-added, 2nd time, escalate'." |
| 2:30-2:45 | **Proof scorecard** | Numbers below | "We simulated 365 days across 6 vendors. All six state cards together went from **1,542 to 1,811 tokens** (max 1,854). Naive full history ends at **428,956**, which is **237x** and passes Liquid's 65k window on **day 56**. The agent caught **5/5** planted violations with 0 missed and 0 false alarms, and discarded **99%** of vendor-days (2,177/2,190) without a prompt." |
| 2:45-3:00 | Back to the graph | Green graph | "Every sponsor carries weight: Nimble is the eyes and the agent's tool, Liquid judges every sentence, Tinybird's RawTree is the memory, OpenAI gives the second opinion. Changeflow and PageCrawl alert on page diffs. We *remember*, for months, at constant cost. Next, the same engine does merchant monitoring for payment facilitators." |

**Proof numbers** (`out/proof.json`, seed 7, offline, regex path, tokens estimated as len/4):

| Claim | Value |
|---|---|
| All 6 state cards, day 1 → day 365 | 1,542 → 1,811 tokens (max 1,854) |
| Naive full history, day 1 → day 365 | 2,034 → 428,956 tokens, 237x (computed, not run) |
| Naive passes the 65,536-token window | day 56 |
| Alerts vs ground truth | 5/5 matched, 0 missed, 0 unexpected |
| Discarded without a prompt or alert | 2,177 / 2,190 vendor-days (99%) |
| Audit trail | 30 memory ops for 4,380 tick logs |
| Ledger ablation (day-45 re-add) | with the ledger: "re-added ... 2nd time, Escalate"; without it: plain "added"; the ledger costs 136 tokens |
| FileStore crash test | 8 crashes, 8 recoveries, 0 missing ids, 0 dup ids, final cards identical |

## 3. Fallbacks

| If | Then |
|---|---|
| Tunnel or Nimble is down | The fetch falls back to a direct request, and the snapshot is labeled `direct, Nimble <error>`. Say so out loud. Or skip the console and run the terminal cue card below. |
| RawTree is slow | `uv run python -m sept25_build.agent.sim --days 90` and then `uv run python -m sept25_build.agent.proof`. Narrate from the output. |
| LLM is rate-limited | The rules still decide R1/R2/R3. Alerts say "Judged by rules (LLM unavailable)." Say "the rules are the floor, the model is the upgrade." |
| Console is broken | Run the terminal cue card below. |
| Stage memory is polluted | Use a fresh suffix: `export NW_TABLE_SUFFIX=_stage2`, `inject.py reset`, then run the baseline tick **twice** (all vendors: `runner --fetch --agent --cycles 1`; the first creates the tables) before the first inject, and start the console with the same suffix. |

**Terminal cue card.** Run the tick after every step:

```bash
tick() { uv run python -m sept25_build.agent.runner --fetch --vendors tinybird --agent --narrate --cycles 1; }   # works in zsh and bash
uv run python site/inject.py status                     # all green
uv run python site/inject.py touch --vendor tinybird && tick       # 0 alerts, discarded
uv run python site/inject.py add-subprocessor --vendor tinybird --name "DataHarvest Ltd" --country "" --url /companies/dataharvest && tick   # R1, found by the agent
uv run python site/inject.py change-policy --vendor tinybird --clause training && tick   # R2
uv run python site/inject.py reset && tick                           # resolved R1 + R2
uv run python -m sept25_build.agent.proof                             # scorecard
```

The simpler step 3 is `--country Singapore` with no `--url`. It fires R1 without the agent episode. Use it if Nimble is slow on the company page.

**After the demo:** run `uv run python site/inject.py reset`.

## 4. Honest claims (don't overstate)

> - **The naive tokens are computed, not run.** It's the same tokenizer on the full appended history, and nobody sent a 428k-token prompt.
> - **The chaos/crash test runs on FileStore** (the local file store), not on RawTree.
> - **The mirrors are illustrative demo pages, not the vendors' real terms.** Every mirror is labeled "(demo mirror)". Never imply a sponsor changed its terms.
> - The proof run is offline and uses the regex judge. The live Liquid and GPT-5.6 Sol judgments are what you see in the console ticks.
