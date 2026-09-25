# Night's Watch — Long-Horizon Vendor Compliance Agent

> **One-liner:** Night's Watch maps a company's full data supply chain — its vendors and *their* sub-processors — and watches it for months at constant cost, flagging the moment a vendor change makes you non-compliant.

Built for the [Long Horizon Agents Hackathon](https://luma.com/horizonagentshack) (SF, 2026-09-25). Team of 3, ~4 hours of build time.

---

## 1. The problem

Every company hands its data to dozens of SaaS vendors. Those vendors hand it on to their own **sub-processors** (fourth parties). Under GDPR Art. 28, processors must notify customers of sub-processor changes and give them a chance to object — in practice that means a quietly edited web page or an email nobody reads.

So a vendor adds a sub-processor in a new country, or edits its privacy policy to allow training AI models on customer data, and the company is out of compliance without knowing it.

## 2. Why this fits the long-horizon theme

The hackathon theme: long-horizon agents break down as observations, actions, and stale context pile up. The call is for *"explicit mutable state instead of ever-growing histories, agents that edit their own working context, and a clear split between what needs to persist and what can be discarded."*

Night's Watch is that architecture applied to a real workload:

| Theme requirement | Night's Watch |
|---|---|
| Explicit mutable state | Each vendor has a small **state card** (~1–2k tokens) that the agent **rewrites** every tick — never appends to |
| Discard what doesn't matter | **Liquid** triages every snapshot; cosmetic changes (dates, formatting) are dropped |
| Persist what matters | Full raw history lives in **Tinybird**, outside the model's context; only material changes update the card |
| Reliable from minutes → months | Context per tick = state card + current diff. **Tokens per tick stay flat** while a naive history-appending agent grows without bound |

**Headline chart:** tokens per tick — Night's Watch (flat) vs. naive baseline (growing).

## 3. The demo story

**Night's Watch Inc.** is a fictional company whose vendors are the hackathon sponsors: **AWS, Nimble, Tinybird, Liquid AI, Black Forest Labs**.

1. Night's Watch maps the supply chain: Night's Watch Inc. → 5 vendors → their sub-processors. All green.
2. It keeps watching. Most ticks: "no material change," discarded. Token chart stays flat.
3. **Live injection:** a teammate adds a dummy sub-processor ("DataHarvest Ltd", in a country outside the company's allowed list) and/or edits a privacy-policy clause to permit AI training.
4. Next tick: Nimble fetches → Liquid flags "material" → Bedrock checks against policy → node turns **red**:
   > ⚠️ **Tinybird (demo mirror)** added **DataHarvest Ltd ([country])** — violates **R1: EU data residency**. Night's Watch Inc. is no longer compliant.

### Injection approach
We can't edit real vendor pages. We **mirror one vendor's sub-processor + privacy pages to our own S3** and inject into the mirror. It is **always labeled "demo mirror" on screen** so no one mistakes it for a real change by the vendor.

## 4. Night's Watch Inc.'s data policy (the compliance rules)

| Rule | Description |
|---|---|
| **R1** | EU customer data stays in the EU or countries the EU has approved for data transfers |
| **R2** | No vendor or sub-processor may use our data to train AI models |
| **R3** | Data retention of 90 days or less |

## 5. Architecture

```
            ┌──────────────┐   tick (manual "run now" or schedule)
            │  Scheduler   │─────────────────────────────┐
            └──────────────┘                             ▼
┌─────────────────────────┐  Extract / Crawl   ┌───────────────────┐
│ Vendor pages            │ ─────────────────► │ Nimble            │  observe
│ (real + S3 demo mirror) │                    │ → snapshot JSON   │
└─────────────────────────┘                    └─────────┬─────────┘
                                                         │ append
                                                         ▼
                                               ┌───────────────────┐
                                               │ Tinybird          │  persist
                                               │ snapshots/ticks/  │  (full history,
                                               │ state_cards/alerts│   never in context)
                                               └─────────┬─────────┘
                                   state card + new diff │
                                                         ▼
                                               ┌───────────────────┐
                                               │ Liquid LFM2.5     │  discard
                                               │ noise vs material │
                                               └─────────┬─────────┘
                                                material │
                                                         ▼
                                               ┌───────────────────┐
                                               │ AWS Bedrock       │  update state
                                               │ policy check →    │
                                               │ rewrite card,alert│
                                               └─────────┬─────────┘
                                                         ▼
                                               ┌───────────────────┐
                                               │ Dashboard         │
                                               │ graph · alerts ·  │
                                               │ tokens/tick chart │
                                               └───────────────────┘
```

### Sponsor tools (4 of 5, one more than the required 3)

| Sponsor | Role | Phase |
|---|---|---|
| **Nimble** | Fetch + extract vendor sub-processor/privacy pages into structured JSON | Observe |
| **Liquid AI** (LFM2.5-2.6B) | Cheap per-snapshot triage: noise vs. material change | Discard |
| **Tinybird** | Full snapshot/tick history + endpoints powering the dashboard | Persist |
| **AWS** (Bedrock + S3) | Policy check, state-card rewrite, alert text; S3 hosts the demo mirror | Update state |
| ~~Black Forest Labs~~ | Image generation — no real role in a text-based compliance product. Appears only as a *monitored vendor*. | — |

**Fallback:** if Liquid can't run locally within 45 minutes, use a small Bedrock model for triage (still 3 sponsors: Nimble, Tinybird, AWS).

## 6. Data formats (the contract between team members)

```jsonc
// snapshot — written by the ingest step (Person A)
{
  "vendor": "tinybird",
  "url": "https://…/subprocessors",
  "fetched_at": "2026-09-25T14:00:00Z",
  "subprocessors": [{ "name": "…", "purpose": "…", "country": "…" }],
  "policy_text_hash": "sha256:…",
  "policy_clauses": { "training": "…", "retention": "…" }
}

// state_card — owned and rewritten by the agent (Person B)
{
  "vendor": "tinybird",
  "subprocessors": ["…"],
  "regions": ["EU", "US"],
  "clauses": { "training_allowed": false, "retention_days": 30 },
  "status": "green",            // green | red
  "open_findings": [],
  "evidence_ids": [],
  "last_material_change_tick": 12
}

// alert — produced on a policy violation (Person B), displayed by Person C
{
  "vendor": "tinybird",
  "rule_violated": "R1",
  "before": "…",
  "after": "…",
  "evidence_url": "…",
  "explanation": "Added DataHarvest Ltd ([country]); outside approved regions.",
  "tick": 13
}

// tick — one row per agent step (for the token chart)
{ "tick": 13, "agent": "nights_watch|naive", "vendor": "tinybird", "input_tokens": 1450, "material": true }
```

## 7. Team split

### Person A — Data & Nimble ("the eyes")
- Find each sponsor's sub-processor and privacy URLs. **Do this first**; some sponsors may not publish a list. Fallback: the third-party section of their privacy policy.
- Nimble Extract → `snapshot` JSON → Tinybird.
- Build the S3 demo mirror for one vendor.
- Injection script:
  - `inject.py add-subprocessor "DataHarvest Ltd" --country XX`
  - `inject.py change-policy --clause training`
  - `inject.py reset`
- A **"run tick now"** trigger so the demo never waits on a schedule.

### Person B — Agent ("the brain")
- State-card logic: load card → compare with new snapshot → **rewrite** card.
- Liquid triage prompt (noise vs. material).
- Bedrock policy check → `alert` with rule, before/after diff, explanation.
- Naive baseline agent (appends full history) — exists only for the token chart.
- Log each tick's token count to Tinybird.

### Person C — Tinybird, UI, pitch ("the face")
- Tinybird data sources: `snapshots`, `state_cards`, `alerts`, `ticks`.
- Endpoints: `graph_edges`, `latest_alerts`, `tokens_per_tick`.
- One-page dashboard:
  1. Supply-chain graph (Cytoscape.js / vis-network); nodes turn red live.
  2. Alert panel with before/after diff.
  3. Tokens-per-tick chart: Night's Watch vs. naive.
- Owns the demo script, pitch, and **backup screen recording**.

## 8. Timeline (4 hours)

| Time | A | B | C |
|---|---|---|---|
| 0:00–0:15 | *All:* injection approach, policy rules, data formats | | |
| 0:15–1:30 | Nimble extraction for 5 vendors, then demo mirror | State card + triage + policy prompt, tested on hand-written snapshots | Tinybird schemas + graph UI on sample data |
| 1:30–2:15 | **Integration 1:** real snapshots → agent → Tinybird → graph | | |
| 2:15–3:00 | Injection script + tick trigger | Naive baseline + token logging; tune alert wording | Alert panel + token chart wired to endpoints |
| 3:00–3:15 | **Integration 2:** full live injection, 3 runs | | |
| 3:15–4:00 | **Code freeze.** Backup video, rehearse pitch twice | | |

**Cut rule:** anything not working by 3:00 gets cut. Must-haves are **graph + injection + red alert**. Everything else is optional.

## 9. Three-minute demo script

1. **(0:30) Hook:** "Night's Watch Inc. runs on these five vendors — today's sponsors. But who do *they* share our data with?" Graph expands to fourth parties.
2. **(0:30) Long horizon:** "We check this every hour, for months. Most checks show no change and get discarded; only changes that matter are kept." Show flat vs. growing token chart.
3. **(1:00) Live injection:** run `inject.py`, trigger a tick. Nimble → Liquid → Bedrock → node turns red. Read the alert aloud.
4. **(1:00) Close:** "Coris and Changeflow prove the market. Our contribution is the architecture: memory that stays accurate over months at constant cost. Next, the same engine watches merchants for payment processors."

## 10. Landscape — who already does this

Neither vendor monitoring nor merchant monitoring is new as a product. We say so up front and pitch the architecture.

| Company | Type | What they do |
|---|---|---|
| [Changeflow](https://changeflow.com/solutions/vendor-risk-monitoring) | Product | AI feed of vendor trust-page changes (new sub-processors, data location, liability) |
| [PageCrawl](https://pagecrawl.io/blog/subprocessor-list-monitoring-saas-compliance) | Product | Page-change monitoring with sub-processor templates |
| [TOS Tracker](https://tostracker.app/compliance), [tldr](https://toolongdidntread.it.com/), [TermGuard](https://www.termguard.app/) | Indie tools | Terms/privacy change tracking with AI summaries |
| [Apify tools](https://apify.com/virtual-constructs/subprocessor-change-monitor/api) | Indie tools | Pay-per-check sub-processor monitors |
| [Coverbase](https://hackread.com/ai-powered-vendor-risk-management-platforms-saas-companies-2026/), [Rescana](https://www.rescana.com/learn/agentic-ai-tprm/) | Startups | Agent-based vendor risk management |
| [PromptArmor](https://www.promptarmor.com/) | Startup | Risk intelligence for AI vendors / AI sub-processors |
| [Hyperproof](https://hyperproof.io/resource/hyperproof-launches-ai-native-third-party-risk-management/), Drata, Vanta, OneTrust | Established | Vendor risk platforms adding continuous AI monitoring |
| [Coris](https://www.coris.ai/) (YC W23) | Startup | Merchant risk monitoring for payment processors (our "next market") |

**Our gap:** existing tools send one-off page-change alerts or run questionnaires. None pitch a **stateful agent whose memory builds up over time at constant cost** — which lets it answer questions like "which of my vendors now route data to a US sub-processor?" or "this is the vendor's third retention change this year."

## 11. Future: same engine, bigger market

**Merchant monitoring for payment facilitators and acquirers.** Visa and Mastercard risk programs (VIRP/VAMP, BRAM) penalize the acquirer, not the merchant. Merchants drift after onboarding (new banned products, transaction laundering) and actively hide it from scanners — where Nimble's residential, location-specific fetching matters most. Same state-card engine, different pages.

## 12. Risks & open questions

- [ ] Do the sponsors publish sub-processor lists? (Check in the first 15 minutes.) If not, use well-known vendors (Slack, Notion, Stripe) or privacy-policy third-party sections.
- [ ] Liquid local setup time on venue wifi — ask the Liquid table about a hosted endpoint.
- [ ] Bedrock model access enabled on the team AWS account.
- [ ] Nimble credits/API key from the sponsor.
- [ ] The naive baseline shows a guaranteed cost/latency gap; only claim an accuracy gap if it actually appears.
