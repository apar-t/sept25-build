# Notes for coding agents

Night's Watch, a hackathon project built by 3 people: apar-t, rohitenterprise and vroy2008. Submission deadline: 16:30 PDT, 2026-09-25.
- Plan: [docs/NIGHTS_WATCH.md](docs/NIGHTS_WATCH.md)
- Sponsor tool setup and gotchas: [docs/sponsors.md](docs/sponsors.md)

## Secrets: do this first
`.env` is gitignored. The repo holds it only encrypted, as `.env.age`, and the keys that can decrypt it are listed in `.age-recipients`.

```bash
brew install age
age -d -i ~/.ssh/id_ed25519 .env.age > .env
```

If you get **"no identity matched any of the recipients"**, the SSH key on this machine is not the one on the user's GitHub account.
- Compare `ssh-keygen -y -f ~/.ssh/id_ed25519` with `curl https://github.com/<user>.keys`.
- To fix it, add this machine's public key (`~/.ssh/id_ed25519.pub`) to `.age-recipients` and ask someone who can already decrypt to re-encrypt. Pushing the updated recipients file alone does nothing.

If the SSH key has a passphrase, age will prompt for it, so ask the user to run the command in their own terminal.

Rules:
- Never commit `.env`. Never print key values in chat, logs or code.
- After editing `.env`, re-encrypt and commit `.env.age`:
  ```bash
  age -R .age-recipients -a -o .env.age .env
  ```
- The keys are shared and full-access. The RawTree key can read and write the whole account. BFL spends real credits, about $0.03 per image, so don't generate images in loops.
- Nimble and AWS Bedrock keys are not in `.env` yet. Ask the user to get them; don't create accounts.

## Plan vs what's set up
- **"Tinybird" in the plan means RawTree** (rawtree.com, a Tinybird product). We have a RawTree key, not a Tinybird Forward token, so don't run `tb login`.
  - Use the RawTree HTTP API with the `RAWTREE_API_KEY` / `RAWTREE_DATABASE` headers. See docs/sponsors.md.
- RawTree database is `default`. The key can't create databases, and other people's tables already live there, so **prefix every table with `nw_`** (`nw_snapshots`, `nw_state_cards`, `nw_alerts`, `nw_ticks`).
- Inserting into a new table creates it automatically. Queries are read-only SQL.

## Lanes: who owns what (read before editing anything)
Each person runs their own coding agent. **Agents edit only their own lane.** If your task needs a change in someone else's lane, stop and tell your human; don't edit it yourself.

| Lane | Owner | Owns | Must provide to others |
|---|---|---|---|
| **A: ingest** | _claim: put your GitHub handle here_ | `src/sept25_build/ingest/`, `scripts/inject.py`, the demo mirror page | `ingest.run_once() -> list[Snapshot]`, written to `nw_snapshots` |
| **B: agent** | apar-t | `src/sept25_build/agent/`, `scripts/tick.py` | `agent.run_tick()`: writes `nw_state_cards`, `nw_alerts`, `nw_ticks` |
| **C: data + web** | _claim: put your GitHub handle here_ | `web/`, `src/sept25_build/rawtree.py`, RawTree queries/endpoints, pitch + backup video | the dashboard |
| **Shared** | everyone | `src/sept25_build/contracts.py`, `AGENTS.md`, `docs/`, `pyproject.toml` | |

- **`contracts.py` is the interface between lanes.** Import `Snapshot`, `StateCard`, `Alert`, `TickLog`, `TABLES`, `RULES` from it. Never redefine these shapes locally. Adding an optional field is fine (say so in the commit message); renaming or removing one needs the team's agreement first.
- **Build against fixtures, not each other.** `fixtures/snapshots/` has synthetic snapshots, including a noise-only change and three injected violations (see `fixtures/README.md`). Nobody should be blocked waiting on another lane.
- **Dependencies:** changing `pyproject.toml`/`uv.lock` conflicts easily. Pull first, add the dependency, commit it on its own, push right away.
- **Collisions:** if `git pull --rebase` conflicts in a file outside your lane, keep the upstream (other person's) version, which during a rebase is `git checkout --ours <file>`, then tell your human what you dropped. Never force-push.
- Commit small and often (every 30–45 min), with messages that say which lane: `B: triage prompt`.

## Notes for lane A (ingest), agreed with B
The agent (lane B) reads `nw_snapshots` and nothing else from A. To keep the demo reliable:
1. **Build `contracts.Snapshot`**, write with `rawtree.insert("snapshots", [s.to_row() for s in snaps])`. Match `fixtures/snapshots/*.json`.
2. **Vendor slugs are fixed:** `aws`, `nimble`, `tinybird`, `liquid`, `bfl` (see `contracts.VENDORS`). The injectable copy is `<vendor>-mirror`, `is_demo_mirror=True`, `display_name="<Vendor> (demo mirror)"`.
3. **`subprocessors`: name / purpose / country exactly as the page shows them.** B normalizes countries. If a vendor publishes no list, send `[]`; never invent entries.
4. **`policy_text`: the privacy/DPA page as markdown, paragraphs separated by blank lines, same extraction format on every fetch.** Strip nav, cookie banners, footers. B compares paragraph by paragraph; unstable formatting costs LLM calls.
5. **Only insert after a real fetch, with a fresh `fetched_at`.** A failed fetch should insert nothing (an empty list or empty policy is treated as a failed fetch and ignored).
6. **`run_once(vendors: list[str] | None = None)`** so the demo can re-fetch only the mirror after an injection.
7. **The mirror page must be public and uncached**, so Nimble sees the injection immediately. Injections can reword, append or delete sentences/paragraphs; B handles all three.
8. **One level only: don't crawl sub-processors' own sub-processor pages.** Deeper links come from name matching on B's side ("link, don't crawl"): a sub-processor whose name matches `contracts.VENDOR_ALIASES` (e.g. "Amazon Web Services, Inc." in Tinybird's list) becomes an edge to the AWS node we already watch. Each company is one node and traversal keeps a visited set, so cycles (Tinybird → AWS → Tinybird) can't loop. If you see a sponsor listed under a name the aliases miss, add it to `VENDOR_ALIASES` and say so in the commit.
9. **Handshake:** once one real snapshot is in `nw_snapshots`, B runs `uv run scripts/tick.py --no-fetch`.

## Build rules
- One tick = ingest (A) → agent (B) → dashboard reads (C). `uv run scripts/tick.py` runs a full tick; the demo triggers it manually, never on a timer.
- The demo mirror is the **only** page we inject into. It must always display as "(demo mirror)"; never imply a real vendor changed its terms.
- Keep the agent's per-tick context constant: state card + current change only. Full history stays in RawTree. This is the hackathon theme, so don't "just add the history to the prompt".
- LLM: Liquid LFM2.5 on the local llama-server (`LFM_BASE_URL`). Bedrock is optional, only if AWS keys arrive.

## Setup
```bash
brew install uv llama.cpp age
uv sync
llama-server -hf LiquidAI/LFM2.5-2.6B-GGUF:Q4_K_M --jinja --port 8080 \
  -c 32768 -fa on -ngl 99 --temp 0.1 --top-k 50 --repeat-penalty 1.1   # ~1.7 GB first run
uv run scripts/smoke_liquid.py && uv run scripts/smoke_bfl.py
```

## Git
Three people push to `main`. Run `git pull --rebase` before every push, and don't force-push.
