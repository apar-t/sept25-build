#!/usr/bin/env bash
# Copy lane B's check code (a snapshot of src/sept25_build) plus run_check.py and the workflow into the private repo
# vroy2008/nights-watch-live, where GitHub Actions runs live company checks. Re-run after lane B changes the check.
set -euo pipefail
root=$(cd "$(dirname "$0")/../.." && pwd)
d=$(mktemp -d)
git clone -q https://github.com/vroy2008/nights-watch-live.git "$d"
rm -rf "$d/src" && mkdir -p "$d/src" "$d/.github/workflows"
rsync -a --exclude __pycache__ "$root/src/sept25_build" "$d/src/"
cp "$root/pyproject.toml" "$root/uv.lock" "$root/.python-version" "$root/web/live/run_check.py" "$d/"
cp "$root/web/live/check.yml" "$d/.github/workflows/check.yml"
printf '# Night'"'"'s Watch live checks\n\nGitHub Actions runs a live check of a company'"'"'s sub-processor list for the demo on nightswatch.app.\nGenerated from sept25-build (web/live/sync.sh, at %s); edit there, not here.\n' "$(git -C "$root" rev-parse --short HEAD)" > "$d/README.md"
git -C "$d" add -A
if git -C "$d" diff --cached --quiet; then echo "already up to date"; else
  git -C "$d" commit -qm "Sync from sept25-build $(git -C "$root" rev-parse --short HEAD)" && git -C "$d" push -q origin HEAD:main && echo synced; fi
