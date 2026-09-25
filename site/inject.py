"""Inject changes into the demo mirror data in site/live/ (served by site/serve.py). Stdlib only.

    uv run python site/inject.py add-subprocessor --vendor tinybird --name "DataHarvest Ltd" --country Singapore
    uv run python site/inject.py add-subprocessor --vendor tinybird --name "DataHarvest Ltd" --country "" --url /companies/dataharvest
    uv run python site/inject.py remove-subprocessor --vendor tinybird --name "DataHarvest Ltd"
    uv run python site/inject.py change-policy --vendor tinybird --clause training|retention
    uv run python site/inject.py append-clause --vendor tinybird --clause training|retention
    uv run python site/inject.py touch --vendor tinybird          # noise: bump "Last updated:"
    uv run python site/inject.py reset [--vendor tinybird]        # back to seed
    uv run python site/inject.py status
"""

import argparse
import json
import os
import re
import shutil
import sys
from datetime import date
from pathlib import Path

SITE = Path(__file__).resolve().parent
SEED, LIVE = SITE / "seed", SITE / "live"
SLUGS = ["aws", "nimble", "tinybird", "liquid", "bfl"]

ORIGINAL = {
    "training": "We do not use customer data to train machine learning or AI models.",
    "retention": "Customer data is deleted within 30 days of account termination.",
}
VIOLATION = {
    "training": "We may use customer data, including content you submit, to improve our products and train our machine learning models.",
    "retention": "Customer data is retained for up to 24 months after account termination.",
}
try:  # the company's real approved list, if the project package is importable
    from sept25_build.contracts import APPROVED_COUNTRIES
except Exception:
    APPROVED_COUNTRIES = ["United States", "Ireland", "Germany", "Netherlands", "Spain", "France",
                          "United Kingdom", "Sweden", "Canada", "Japan"]
APPROVED = {c.lower() for c in APPROVED_COUNTRIES}


def path(slug: str) -> Path:
    return LIVE / "vendors" / f"{slug}.json"


def ensure_live() -> None:
    for src in SEED.rglob("*.json"):
        dst = LIVE / src.relative_to(SEED)
        if not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)


def load(slug: str) -> dict:
    p = path(slug)
    if not p.exists():
        sys.exit(f"unknown vendor {slug!r}; choose from {', '.join(SLUGS)}")
    return json.loads(p.read_text())


def save(v: dict) -> None:
    """Atomic write, so the server never reads a half-written file."""
    p = path(v["slug"])
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(v, indent=2, ensure_ascii=False) + "\n")
    os.replace(tmp, p)


def add_subprocessor(a) -> None:
    v = load(a.vendor)
    if any(s["name"].lower() == a.name.lower() for s in v["subprocessors"]):
        print(f"{a.vendor}: {a.name!r} is already listed; nothing changed")
        return
    entry = {"name": a.name, "purpose": a.purpose, "country": a.country.strip()}
    if a.url:
        entry["url"] = a.url.strip()  # a site path like /companies/x is served as <request base>/companies/x
    v["subprocessors"].append(entry)
    save(v)
    where = entry["country"] or "location unknown"
    link = f"; url {entry['url']}" if a.url else ""
    print(f"{a.vendor}: added sub-processor {a.name!r} ({a.purpose}; {where}{link}) -> {len(v['subprocessors'])} total")


def remove_subprocessor(a) -> None:
    v = load(a.vendor)
    keep = [s for s in v["subprocessors"] if s["name"].lower() != a.name.lower()]
    if len(keep) == len(v["subprocessors"]):
        print(f"{a.vendor}: no sub-processor named {a.name!r}; nothing changed")
        return
    v["subprocessors"] = keep
    save(v)
    print(f"{a.vendor}: removed sub-processor {a.name!r} -> {len(keep)} total")


def change_policy(a) -> None:
    v = load(a.vendor)
    old, new = ORIGINAL[a.clause], VIOLATION[a.clause]
    if old not in v["dpa_markdown"]:
        print(f"{a.vendor}: original {a.clause} sentence not found (already changed?); nothing changed")
        return
    v["dpa_markdown"] = v["dpa_markdown"].replace(old, new)
    save(v)
    print(f"{a.vendor}: reworded {a.clause} clause\n  - {old}\n  + {new}")


def append_clause(a) -> None:
    v = load(a.vendor)
    new = VIOLATION[a.clause]
    md = v["dpa_markdown"]
    if new in md:
        print(f"{a.vendor}: {a.clause} violation already present; nothing changed")
        return
    paras = re.split(r"\n\s*\n", md.strip())
    idx = next((i for i, p in enumerate(paras) if ORIGINAL[a.clause] in p), len(paras) - 1)
    paras.insert(idx + 1, new)  # new paragraph right after the original clause's paragraph
    v["dpa_markdown"] = "\n\n".join(paras) + "\n"
    save(v)
    print(f"{a.vendor}: appended new {a.clause} paragraph (original kept)\n  + {new}")


def touch(a) -> None:
    v = load(a.vendor)
    today = date.today().isoformat()
    md, n = re.subn(r"Last updated: \d{4}-\d{2}-\d{2}", f"Last updated: {today}", v["dpa_markdown"])
    if not n:
        print(f"{a.vendor}: no 'Last updated:' line found; nothing changed")
        return
    v["dpa_markdown"] = md
    save(v)
    print(f"{a.vendor}: set 'Last updated: {today}' (noise only)")


def reset(a) -> None:
    slugs = [a.vendor] if a.vendor else SLUGS
    for s in slugs:
        src = SEED / "vendors" / f"{s}.json"
        if not src.exists():
            sys.exit(f"unknown vendor {s!r}; choose from {', '.join(SLUGS)}")
        path(s).parent.mkdir(parents=True, exist_ok=True)
        tmp = path(s).with_suffix(".json.tmp")
        shutil.copy2(src, tmp)
        os.replace(tmp, path(s))
    if not a.vendor:
        for f in ("company.json", "companies.json"):
            shutil.copy2(SEED / f, LIVE / f)
    print(f"reset to seed: {', '.join(slugs)}")


def clause_state(md: str, clause: str) -> str:
    has_old, has_new = ORIGINAL[clause] in md, VIOLATION[clause] in md
    if has_old and not has_new:
        return "original"
    if has_old and has_new:
        return "APPENDED"
    if has_new:
        return "CHANGED"
    return "MISSING"


def status(_a) -> None:
    for s in SLUGS:
        if not path(s).exists():
            print(f"{s:9s} (missing)")
            continue
        v = load(s)
        bad = [f"{sp['name']} ({sp['country']})" for sp in v["subprocessors"]
               if sp["country"].strip() and sp["country"].strip().lower() not in APPROVED]
        unknown = [sp["name"] + (f" (url {sp['url']})" if sp.get("url") else "")
                   for sp in v["subprocessors"] if not sp["country"].strip()]
        md = v["dpa_markdown"]
        upd = re.search(r"Last updated: (\S+)", md)
        print(f"{s:9s} subprocessors={len(v['subprocessors'])}  non-approved={', '.join(bad) or 'none'}  "
              + (f"location-unknown={', '.join(unknown)}  " if unknown else "") +
              f"training={clause_state(md, 'training')}  retention={clause_state(md, 'retention')}  "
              f"last_updated={upd.group(1) if upd else '?'}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Inject changes into the Night's Watch demo mirror (site/live/).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("add-subprocessor"); p.add_argument("--vendor", required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--country", required=True, help='may be "" (no listed location)')
    p.add_argument("--url", default="", help="website; a path like /companies/dataharvest is served as an absolute URL")
    p.add_argument("--purpose", default="Data enrichment and analytics"); p.set_defaults(fn=add_subprocessor)
    p = sub.add_parser("remove-subprocessor"); p.add_argument("--vendor", required=True)
    p.add_argument("--name", required=True); p.set_defaults(fn=remove_subprocessor)
    for name, fn in [("change-policy", change_policy), ("append-clause", append_clause)]:
        p = sub.add_parser(name); p.add_argument("--vendor", required=True)
        p.add_argument("--clause", required=True, choices=["training", "retention"]); p.set_defaults(fn=fn)
    p = sub.add_parser("touch"); p.add_argument("--vendor", required=True); p.set_defaults(fn=touch)
    p = sub.add_parser("reset"); p.add_argument("--vendor"); p.set_defaults(fn=reset)
    p = sub.add_parser("status"); p.set_defaults(fn=status)
    a = ap.parse_args()
    ensure_live()
    a.fn(a)


if __name__ == "__main__":
    main()
