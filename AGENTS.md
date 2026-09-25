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
