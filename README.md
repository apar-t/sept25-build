# sept25-build

**Night's Watch**: long-horizon vendor compliance agent. See [docs/NIGHTS_WATCH.md](docs/NIGHTS_WATCH.md).

Sponsor tool setup notes and gotchas: [docs/sponsors.md](docs/sponsors.md).

## Setup (macOS)

```bash
brew install uv llama.cpp age
uv sync
```

### Secrets
`.env` is committed only in encrypted form, as `.env.age`. Only the keys listed in `.age-recipients` can decrypt it.

```bash
# apar-t (GitHub SSH key)
age -d -i ~/.ssh/id_ed25519 .env.age > .env
# vroy2008
age -d -i ~/.config/age/keys.txt .env.age > .env
```

After changing `.env`, re-encrypt and commit the result:
```bash
age -R .age-recipients -a -o .env.age .env
```

To give someone access, add their public key to `.age-recipients`, then re-encrypt. Their GitHub SSH key works: `curl https://github.com/<user>.keys`.

### Liquid: local LFM2.5-2.6B
The first run downloads about 1.7 GB.
```bash
llama-server -hf LiquidAI/LFM2.5-2.6B-GGUF:Q4_K_M --jinja --port 8080 \
  -c 32768 -fa on -ngl 99 --temp 0.1 --top-k 50 --repeat-penalty 1.1
```

### Smoke tests
```bash
uv run scripts/smoke_liquid.py          # tool call against the local LFM
uv run scripts/smoke_bfl.py             # BFL key and credit balance (free)
uv run scripts/smoke_bfl.py --generate  # one FLUX image, about $0.03
```
