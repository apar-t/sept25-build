"""Pre-stage checklist for the live demo. Read-only: never writes RawTree or site/live.

    uv run python -m sept25_build.agent.preflight          # no LLM / Nimble calls
    uv run python -m sept25_build.agent.preflight --warm   # + 1 Liquid call, 1 reviewer call, 1 Nimble fetch

Prints PASS / WARN / FAIL per check with one fallback line each, then the stage cue card.
Exits 1 if anything FAILs. Env keys are reported by NAME only, never by value.
"""

import argparse
import importlib.util
import inspect
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parents[3]
SITE = ROOT / "site"
LOCAL = "http://127.0.0.1:8765"
PROBE = "/vendors/tinybird/subprocessors.json"
MIRROR = "tinybird-mirror"
DH_KEY = "dataharvestltd"   # policy.sub_key("DataHarvest Ltd")
REQUIRED_ENV = ["RAWTREE_API_KEY", "RAWTREE_DATABASE"]
WANTED_ENV = ["OPENROUTER_API_KEY", "NIMBLE_API_KEY"]

results: list[str] = []


def report(status: str, name: str, detail: str, fallback: str) -> None:
    results.append(status)
    print(f"{status:4}  {name:9} {detail}")
    print(f"      -> {fallback}")


def ago(seconds: float) -> str:
    s = int(max(seconds, 0))
    return f"{s // 3600}h{s % 3600 // 60:02d}m" if s >= 3600 else f"{s // 60}m{s % 60:02d}s"


def load_inject():
    spec = importlib.util.spec_from_file_location("site_inject", SITE / "inject.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def check_env() -> None:
    missing = [k for k in REQUIRED_ENV if not os.environ.get(k)]
    soft = [k for k in WANTED_ENV if not os.environ.get(k)]
    present = [k for k in REQUIRED_ENV + WANTED_ENV if os.environ.get(k)]
    detail = f"present: {', '.join(present) or 'none'}" + (f"; missing: {', '.join(missing + soft)}" if missing + soft else "")
    status = "FAIL" if missing else "WARN" if soft else "PASS"
    report(status, "env", detail, "decrypt keys: age -d -i ~/.ssh/id_ed25519 .env.age > .env "
           "(no NIMBLE -> direct fetch; no OPENROUTER -> regex judge, no reviewer)")


def check_site(inj) -> None:
    try:
        r = requests.get(LOCAL + PROBE, timeout=5)
        rows = r.json() if r.ok else None
        ok = r.ok and isinstance(rows, list)
        detail = (f"GET {PROBE} -> {r.status_code}, {len(rows)} sub-processors, "
                  f"Cache-Control: {r.headers.get('Cache-Control', 'MISSING')}" if ok else f"GET {PROBE} -> {r.status_code}")
        report("PASS" if ok else "FAIL", "site", detail, "start it: uv run python site/serve.py (or bash site/tunnel.sh)")
    except (requests.RequestException, ValueError) as e:
        report("FAIL", "site", f"{LOCAL} unreachable: {type(e).__name__}", "start it: uv run python site/serve.py (or bash site/tunnel.sh)")

    dirty = []
    for s in inj.SLUGS:
        if not inj.path(s).exists():
            dirty.append(f"{s}: missing")
            continue
        v = inj.load(s)
        subs = v["subprocessors"]
        bad = [sp["name"] for sp in subs if sp["country"].strip() and sp["country"].strip().lower() not in inj.APPROVED]
        unknown = [sp["name"] for sp in subs if not sp["country"].strip()]
        md = v["dpa_markdown"]
        clauses = {c: inj.clause_state(md, c) for c in ("training", "retention")}
        issues = ([f"non-approved={','.join(bad)}"] if bad else []) + ([f"location-unknown={','.join(unknown)}"] if unknown else []) \
            + [f"{c}={st}" for c, st in clauses.items() if st != "original"]
        seed = inj.SEED / "vendors" / f"{s}.json"
        if not issues and seed.exists() and seed.read_text() != inj.path(s).read_text():
            issues.append("differs from seed (touched?)")
        if issues:
            dirty.append(f"{s}: {' '.join(issues)}")
    if dirty:
        status = "FAIL" if any("touched" not in d for d in dirty) else "WARN"
        report(status, "seed", "site/live is NOT at seed: " + "; ".join(dirty), "uv run python site/inject.py reset")
    else:
        report("PASS", "seed", f"site/live matches seed for {', '.join(inj.SLUGS)} (all green)",
               "if a rehearsal left it dirty: uv run python site/inject.py reset")


def check_tunnel() -> str:
    f = SITE / "live" / "tunnel_url.txt"
    if not f.exists():
        report("FAIL", "tunnel", "site/live/tunnel_url.txt missing", "bash site/tunnel.sh, then send lane A the new URL")
        return ""
    base = f.read_text().strip().rstrip("/")
    age = ago(time.time() - f.stat().st_mtime)
    try:
        t0 = time.monotonic()
        r = requests.get(base + PROBE, timeout=15)
        ms = (time.monotonic() - t0) * 1000
        local = requests.get(LOCAL + PROBE, timeout=5).json()
        remote = r.json() if r.ok else None
        same = isinstance(remote, list) and [x["name"] for x in remote] == [x["name"] for x in local]
        detail = (f"{base} (file age {age}) {PROBE} -> {r.status_code} in {ms:.0f}ms"
                  + (", same list as local" if same else ", DIFFERS from local"))
        report("PASS" if same else "FAIL", "tunnel", detail, "bash site/tunnel.sh restarts it; the URL changes, so resend it to lane A")
    except (requests.RequestException, ValueError, KeyError, TypeError) as e:
        report("FAIL", "tunnel", f"{base} (file age {age}) unreachable: {type(e).__name__}",
               "bash site/tunnel.sh restarts it; the URL changes, so resend it to lane A")
    print(f"      lane A must fetch THIS base URL: {base}")
    return base


def check_llm() -> None:
    from . import llm
    desc = llm.describe()
    rev = f"reviewer {llm.REVIEW_MODEL} {'ready' if llm.review_available() else 'UNAVAILABLE'}"
    bad = "UNAVAILABLE" in desc
    report("WARN" if bad or not llm.review_available() else "PASS", "llm",
           f"backend={llm.BACKEND}: {desc}; {rev}",
           "if Liquid is down the regex verdicts still fire R2/R3; LLM_BACKEND=local uses llama-server on :8080")


def check_rawtree():
    from .store import RawTreeStore
    try:
        t0 = time.monotonic()
        cards = RawTreeStore().latest_cards()
        ms = (time.monotonic() - t0) * 1000
    except Exception as e:
        report("FAIL", "rawtree", f"latest_cards() failed: {type(e).__name__}: {str(e)[:120]}",
               "check RAWTREE_API_KEY / network; offline demo: uv run python -m sept25_build.agent.sim --days 90")
        return None
    report("PASS", "rawtree", f"latest_cards() -> {len(cards)} cards in {ms:.0f}ms" + (" (nw_state_cards empty or not created yet)" if not cards else ""),
           "if RawTree is slow on stage, narrate the sim instead: uv run python -m sept25_build.agent.sim --days 90")
    return cards


def check_mirror(cards) -> str:
    """Returns the R1 title the stage will see for the DataHarvest move."""
    from .core import _ordinal
    card = (cards or {}).get(MIRROR)
    name = card.display_name if card else "Tinybird (demo mirror)"
    where_nc = "Singapore, found by the agent"
    if cards is None:
        return f"{name} added DataHarvest Ltd ({where_nc})"
    if card is None:
        report("WARN", "mirror", f"no state card for {MIRROR} yet: the first tick will say 'Baseline: ...', not 'added'",
               "run one clean tick before going on stage (runner --fetch --cycles 1)")
        return f"{name} added DataHarvest Ltd ({where_nc})"
    findings = [f"{f.rule}" for f in card.open_findings]
    ok = card.status == "green" and not findings
    report("PASS" if ok else "FAIL", "mirror",
           f"{MIRROR} tick {card.tick} status={card.status} open_findings={findings or 'none'}",
           "reset the site (site/inject.py reset) and run one tick so the mirror goes green before the demo")
    led, inv = card.ledger.get(DH_KEY), card.investigations.get(DH_KEY)
    title = f"{name} added DataHarvest Ltd ({where_nc})"
    if led and led.get("last_removed") is not None:
        title = f"{name} re-added DataHarvest Ltd ({where_nc}), {_ordinal(led.get('times_added', 1) + 1)} time"
    if led or inv:
        bits = []
        if led:
            bits.append(f"ledger: first_seen t{led.get('first_seen')}, times_added={led.get('times_added')}, "
                        f"last_removed t{led.get('last_removed')}")
        if inv:
            bits.append(f"investigation: country={inv.get('country') or '?'} at t{inv.get('tick')} ({inv.get('how', '')}), "
                        "so the no-country move reuses it instead of fetching with Nimble")
        report("WARN", "memory", "DataHarvest already in memory: " + "; ".join(bits) + f". Stage title will say: '{title}'",
               "say it on stage: 'it remembers this vendor was removed before' (the recurrence is a feature)")
    else:
        report("PASS", "memory", f"no DataHarvest history. Stage title will say: '{title}'",
               "a second add after a reset will say 're-added ..., 2nd time'")
    return title


def check_lease() -> dict | None:
    from .runner import LEASE_SECS, Lease
    try:
        cur = Lease("rawtree", "preflight", "preflight").current()
    except Exception as e:
        report("WARN", "lease", f"could not read lease: {type(e).__name__}", "start a runner with --takeover if none is live")
        return None
    if not cur:
        report("WARN", "lease", "no runner has held the lease yet",
               "uv run python -m sept25_build.agent.runner --fetch --agent")
        return None
    at = datetime.fromisoformat(cur["at"].replace(" ", "T"))
    age = (datetime.now(timezone.utc) - (at if at.tzinfo else at.replace(tzinfo=timezone.utc))).total_seconds()
    live = cur["state"] == "running" and age < LEASE_SECS
    stale = cur["state"] == "running" and not live
    report("WARN" if stale else "PASS", "lease",
           f"holder={cur['holder']} state={cur['state']} heartbeat {ago(age)} ago" + (" (LIVE)" if live else " (stale)" if stale else ""),
           "stale lease: runner ... --takeover; live runner: injections are picked up on its next cycle")
    cur["live"] = live
    return cur


def check_ingest() -> None:
    try:
        from .. import ingest
    except Exception as e:
        report("WARN", "ingest", f"import failed: {type(e).__name__}: {e}", "lane A must insert snapshots after each inject")
        return
    if not hasattr(ingest, "run_once"):
        report("WARN", "ingest", "ingest.run_once missing (lane A): runner --fetch will run the agent only",
               "lane A must fetch the tunnel URL and insert nw_snapshots after each inject")
        return
    try:
        params = inspect.signature(ingest.run_once).parameters
    except (TypeError, ValueError):
        params = {}
    has_v = "vendors" in params
    report("PASS" if has_v else "WARN", "ingest",
           f"ingest.run_once present; accepts vendors={'yes' if has_v else 'NO'}",
           "without vendors= every tick re-fetches all 10 pages (slower); ask lane A for run_once(vendors=[...])")


def warm(base: str) -> None:
    from . import llm
    from .core import SYSTEM, VERDICT_SCHEMA
    sentence = load_inject().VIOLATION["training"]
    user = f"0. {sentence}"
    for label, call in (("warm-lfm", llm.json_call), ("warm-rev", llm.review_call)):
        t0 = time.monotonic()
        try:
            raw, _ = call(SYSTEM, user, VERDICT_SCHEMA)
            ms = (time.monotonic() - t0) * 1000
            items = raw.get("items") or [{}]
            got = items[0].get("allows_training")
            report("PASS" if got is True else "WARN", label, f"allows_training={got} (expect true) in {ms:.0f}ms",
                   "a wrong/failed Liquid verdict is caught by the regex + reviewer; R2 still fires")
        except Exception as e:
            ms = (time.monotonic() - t0) * 1000
            report("WARN", label, f"call failed after {ms:.0f}ms: {type(e).__name__}: {str(e)[:120]}",
                   "regex verdicts still fire R2; check OPENROUTER_API_KEY / rate limits")
    if not base:
        report("WARN", "warm-web", "no tunnel URL, skipped Nimble fetch", "bash site/tunnel.sh")
        return
    from .episode import fetch_page
    url = f"{base}/companies/dataharvest"
    t0 = time.monotonic()
    out = fetch_page(url)
    ms = (time.monotonic() - t0) * 1000
    nimble, sg = out.startswith("[via Nimble]"), "Singapore" in out
    report("PASS" if nimble and sg else "WARN", "warm-web",
           f"fetch_page({url}) in {ms:.0f}ms: via Nimble={'yes' if nimble else 'NO'}, Singapore={'yes' if sg else 'NO'}"
           + ("" if nimble else f" [{out[:80]}]"),
           "if Nimble is down the agent fetches directly; if Singapore is missing use the --country Singapore move")


def cue_card(title: str, lease: dict | None) -> None:
    inj = "uv run python site/inject.py"
    tick = (f"runner {lease['holder']} is LIVE: wait for its next cycle (~30s)" if lease and lease.get("live")
            else "uv run python -m sept25_build.agent.runner --fetch --vendors tinybird --agent --narrate --cycles 1")
    name = title.split(" added ")[0].split(" re-added ")[0]
    steps = [
        (f"{inj} status", "all five vendors green (no tick needed)"),
        (f"{inj} touch --vendor tinybird", "0 alerts: 'Last updated' bump discarded as noise"),
        (f'{inj} add-subprocessor --vendor tinybird --name "DataHarvest Ltd" --country "" --url /companies/dataharvest',
         f"VIOLATION R1  {title}"),
        (f"{inj} change-policy --vendor tinybird --clause training",
         f"VIOLATION R2  {name} now allows training on customer data"),
        (f"{inj} reset",
         f"resolved R1  {name} no longer sends data to DataHarvest Ltd in Singapore; "
         f"resolved R2  {name} no longer allows training on customer data"),
    ]
    print("\n=== STAGE CUE CARD ===")
    print(f"tick after each inject: {tick}")
    for i, (cmd, expect) in enumerate(steps, 1):
        print(f"{i}. {cmd}\n   expect: {expect}")
    print(f'   (simpler step 3: --country Singapore -> VIOLATION R1  {name} added DataHarvest Ltd (Singapore))')


def main() -> None:
    ap = argparse.ArgumentParser(description="Night's Watch pre-stage checklist (read-only)")
    ap.add_argument("--warm", action="store_true", help="also make 1 Liquid call, 1 reviewer call, 1 Nimble fetch")
    args = ap.parse_args()
    print(f"Night's Watch preflight {datetime.now().strftime('%H:%M:%S')}{' --warm' if args.warm else ''}")
    check_env()
    check_site(load_inject())
    base = check_tunnel()
    check_llm()
    cards = check_rawtree()
    title = check_mirror(cards)
    lease = check_lease()
    check_ingest()
    if args.warm:
        warm(base)
    cue_card(title, lease)
    fails, warns = results.count("FAIL"), results.count("WARN")
    print(f"\n{len(results)} checks: {results.count('PASS')} PASS, {warns} WARN, {fails} FAIL")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
