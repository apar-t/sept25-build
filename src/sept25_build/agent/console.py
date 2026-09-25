"""Stage console: one local page with the live supply-chain graph, alerts, agent episode, memory edits,
token chart and the demo's action buttons.

    uv run python -m sept25_build.agent.console [--port 8790]

Binds 127.0.0.1 only. Respects NW_TABLE_SUFFIX (set NW_TABLE_SUFFIX=_e2e to rehearse on the e2e tables);
actions run as subprocesses with this process's env, so they hit the same tables.
Only a fixed set of actions maps to fixed argv lists; no request text ever reaches a command line.
"""

import argparse
import json
import os
import re
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .. import rawtree
from ..contracts import APPROVED_COUNTRIES, TABLES
from . import policy
from .store import RawTreeStore

ROOT = Path(__file__).resolve().parents[3]
SUFFIX = os.environ.get("NW_TABLE_SUFFIX", "")
PY = ["uv", "run", "python"]
INJECT = PY + ["site/inject.py"]
ACTIONS = {
    "tick": PY + ["-m", "sept25_build.agent.runner", "--fetch", "--vendors", "tinybird", "--agent", "--narrate",
                  "--cycles", "1"],
    "touch": INJECT + ["touch", "--vendor", "tinybird"],
    "dataharvest": INJECT + ["add-subprocessor", "--vendor", "tinybird", "--name", "DataHarvest Ltd", "--country", "",
                             "--url", "/companies/dataharvest"],
    "training": INJECT + ["change-policy", "--vendor", "tinybird", "--clause", "training"],
    "reset": INJECT + ["reset"],
    "proof": PY + ["-m", "sept25_build.agent.proof"],
}
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
APPROVED = {c.lower() for c in APPROVED_COUNTRIES}

_lock = threading.Lock()
_cache: dict = {"at": 0.0, "data": None}
_cache_lock = threading.Lock()


def _q(sql: str) -> list[dict]:
    try:
        return rawtree.query(sql)
    except Exception as e:
        print(f"query failed: {type(e).__name__}: {str(e)[:120]}")
        return []


def _int(x) -> int:
    try:
        return int(float(x or 0))
    except (TypeError, ValueError):
        return 0


def _json(s, default):
    try:
        return json.loads(s) if s else default
    except (TypeError, ValueError):
        return default


def _vendors() -> list[dict]:
    try:
        cards = RawTreeStore().latest_cards()
    except Exception as e:
        print(f"latest_cards failed: {type(e).__name__}")
        return []
    out = []
    for v, c in sorted(cards.items()):
        subs = []
        for s in c.subprocessors:
            country, found = s.country.strip(), False
            if not country:
                inv = c.investigations.get(policy.sub_key(s), {})
                if inv.get("country"):
                    country, found = inv["country"], True
            subs.append({"name": s.name, "country": country, "url": s.url, "found_by_agent": found,
                         "approved": (country.lower() in APPROVED) if country else None})
        out.append({
            "vendor": v, "display_name": c.display_name, "status": c.status, "tick": c.tick,
            "open_findings": [{"rule": f.rule, "summary": f.summary} for f in c.open_findings],
            "subprocessors": subs, "depends_on": c.depends_on, "exposed_via": c.exposed_via,
            "notes": c.notes, "counters": c.counters,
            "card_tokens": len(json.dumps(c.prompt_view())) // 4,
        })
    return out


def _alerts() -> list[dict]:
    rows = _q(f"SELECT toString(tick) t, toString(created_at) c, toString(kind) kind, toString(rule) rule, "
              f"toString(title) title, toString(explanation) explanation, toString(evidence_url) evidence_url "
              f"FROM {TABLES['alerts']}")
    rows.sort(key=lambda r: (_int(r["t"]), r["c"]), reverse=True)
    return [{"tick": _int(r["t"]), "kind": r["kind"], "rule": r["rule"], "title": r["title"],
             "explanation": r["explanation"], "evidence_url": r["evidence_url"]} for r in rows[:12]]


def _episodes() -> list[dict]:
    rows = _q(f"SELECT toString(tick) t, toString(created_at) c, toString(vendor) vendor, toString(trigger) trigger, "
              f"toString(model) model, toString(steps) steps, toString(outcome) outcome, "
              f"toString(committed) committed, toString(latency_ms) latency_ms FROM {TABLES['episodes']}")
    rows.sort(key=lambda r: (_int(r["t"]), r["c"]), reverse=True)
    return [{"tick": _int(r["t"]), "vendor": r["vendor"], "trigger": r["trigger"], "model": r["model"],
             "steps": _json(r["steps"], []), "outcome": r["outcome"], "committed": _json(r["committed"], {}),
             "latency_ms": _int(r["latency_ms"])} for r in rows[:4]]


def _memory_ops() -> list[dict]:
    rows = _q(f"SELECT toString(tick) t, toString(created_at) c, toString(vendor) vendor, toString(op) op, "
              f"toString(field) field, toString(before) b, toString(after) a, toString(why) why "
              f"FROM {TABLES['memory_ops']}")
    rows.sort(key=lambda r: (_int(r["t"]), r["c"]), reverse=True)
    return [{"tick": _int(r["t"]), "vendor": r["vendor"], "op": r["op"], "field": r["field"], "before": r["b"],
             "after": r["a"], "why": r["why"]} for r in rows[:25]]


def _series() -> dict:
    rows = _q(f"SELECT toString(tick) t, toString(agent) a, toString(card_tokens) c, toString(input_tokens) i "
              f"FROM {TABLES['ticks']}")
    nw: dict[int, int] = {}
    naive: dict[int, int] = {}
    for r in rows:
        t = _int(r["t"])
        if r["a"] == "nights_watch":
            nw[t] = nw.get(t, 0) + _int(r["c"])
        elif r["a"] == "naive":
            naive[t] = naive.get(t, 0) + _int(r["i"])
    ticks = sorted(set(nw) | set(naive))
    return {"ticks": ticks, "nights_watch": [nw.get(t, 0) for t in ticks],
            "naive": [naive.get(t, 0) for t in ticks], "naive_label": "naive history (computed, not run)"}


def _proof():
    p = ROOT / "out" / "proof.json"
    try:
        return json.loads(p.read_text()) if p.exists() else None
    except Exception:
        return None


def state() -> dict:
    with _cache_lock:
        if _cache["data"] is not None and time.time() - _cache["at"] < 1.5:
            return _cache["data"]
        data = {"suffix": SUFFIX, "at": time.strftime("%H:%M:%S"), "busy": _lock.locked(),
                "vendors": _vendors(), "alerts": _alerts(), "episodes": _episodes(),
                "memory_ops": _memory_ops(), "series": _series(), "proof": _proof()}
        _cache.update(at=time.time(), data=data)
        return data


def run_action(name: str) -> tuple[int, dict]:
    argv = ACTIONS.get(name)
    if argv is None:
        return 400, {"ok": False, "seconds": 0, "output": f"unknown action (allowed: {', '.join(ACTIONS)})"}
    if not _lock.acquire(blocking=False):
        return 409, {"ok": False, "seconds": 0, "output": "another action is still running"}
    t0 = time.time()
    try:
        env = dict(os.environ, NO_COLOR="1", PYTHONUNBUFFERED="1")
        try:
            p = subprocess.run(argv, cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
            ok, out = p.returncode == 0, (p.stdout or "") + (p.stderr or "")
            if not ok:
                out += f"\n[exit code {p.returncode}]"
        except subprocess.TimeoutExpired as e:
            ok = False
            out = ((e.stdout or b"").decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or ""))
            out += "\n[timed out after 120s]"
        except Exception as e:
            ok, out = False, f"failed to start: {type(e).__name__}: {e}"
        with _cache_lock:
            _cache["data"] = None  # next poll shows the new state right away
        return 200, {"ok": ok, "seconds": round(time.time() - t0, 1), "output": ANSI.sub("", out)[-20000:]}
    finally:
        _lock.release()


# ---------------------------------------------------------------------------------------------------------
# B2B app API (/api/overview, /api/onboard, /api/simulate, /api/job/<id>) for src/sept25_build/agent/ui/app.html
# ---------------------------------------------------------------------------------------------------------

APP_PAGE = Path(__file__).resolve().parent / "ui" / "app.html"
TUNNEL_FILE = ROOT / "site" / "live" / "tunnel_url.txt"
POLICY = {"R1": "International transfers", "R2": "AI and model training", "R3": "Retention"}
TICK = PY + ["-m", "sept25_build.agent.runner", "--fetch", "--vendors", "tinybird", "--agent", "--narrate",
             "--cycles", "1"]
SCENARIOS = {
    "new_subprocessor": INJECT + ["add-subprocessor", "--vendor", "tinybird", "--name", "DataHarvest Ltd",
                                  "--country", "", "--url", "/companies/dataharvest"],
    "training_clause": INJECT + ["change-policy", "--vendor", "tinybird", "--clause", "training"],
    "undo": INJECT + ["reset"],
}
_jobs: dict[str, dict] = {}
_ocache: dict = {"at": 0.0, "data": None}


def _site_url() -> str:
    url = os.environ.get("SITE_URL") or (TUNNEL_FILE.read_text() if TUNNEL_FILE.exists() else "")
    return url.strip().rstrip("/")


def _name(vendor: str) -> str:
    from ..contracts import VENDORS
    slug = vendor.removesuffix("-mirror")
    return VENDORS.get(slug, slug)


def _plain(s: str) -> str:
    return re.sub(r"\s*\(demo mirror\)", "", s or "")


def _policy(rule: str) -> dict | None:
    if rule not in POLICY:
        return None
    return {"doc": "Privacy Policy", "section": POLICY[rule], "url": _site_url() + "/privacy"}


def _headline(kind: str, vendor: str) -> str:
    n = _name(vendor)
    return f"{n} is no longer compliant with your Privacy Policy" if kind == "violation" else f"{n} is compliant again"


def _why(a: dict) -> str:
    rule, kind, ex, title = a.get("rule"), a.get("kind"), a.get("explanation") or "", _plain(a.get("title") or "")
    if rule == "R1":
        if kind == "violation":
            m = re.match(r"(.+?) processes data in (.+?), which is not", ex)
            if m:
                s = (f"Its new sub-processor {m.group(1)} processes customer data in {m.group(2)}, "
                     f"which is not an approved country")
                return s + (" (found by the agent on the sub-processor's own website)." if "agent followed" in ex else ".")
        else:
            m = re.match(r"(.+?) no longer processes data in (.+?)\.", ex)
            if m:
                return f"It no longer sends customer data to {m.group(1)} in {m.group(2)}."
    if rule == "R2":
        return ("Its terms now allow training AI models on customer data." if kind == "violation"
                else "Its terms no longer allow training AI models on customer data.")
    if rule == "R3":
        m = re.search(r"(\d+) days", title)
        return (f"It now keeps customer data for {m.group(1) if m else 'more than 90'} days; your policy allows 90 at most."
                if kind == "violation" else "Its data retention is back within 90 days.")
    return title + "."


def _friendly_tool(s: dict) -> dict:
    tool, args, res = s.get("tool", ""), s.get("args") or {}, str(s.get("result") or "")
    if tool == "fetch_page":
        url = args.get("url", "") if isinstance(args, dict) else str(args)
        from urllib.parse import urlparse
        return {"text": f"Opened {urlparse(url).path or url} via Nimble", "detail": res[:200]}
    if tool == "recall":
        return {"text": "Checked its memory of this vendor", "detail": res[:200]}
    if tool == "search_web":
        return {"text": f"Searched the web for {args.get('query', '') if isinstance(args, dict) else ''}".strip(),
                "detail": res[:200]}
    if tool == "judge_sentences":
        return {"text": "Asked Liquid to judge the policy sentences", "detail": res[:200]}
    if tool == "check_fourth_parties":
        return {"text": "Checked its vendors' own vendors", "detail": res[:200]}
    return {"text": tool or "step", "detail": res[:200]}


def _op_phrase(o: dict) -> str:
    op, f, b, a = o["op"], o["field"], _plain(o["before"]), _plain(o["after"])
    short = lambda x: " ".join(str(x).split())[:80]
    if op == "set":
        return f"Changed {f}: {short(b)} -> {short(a)}"
    if op == "open_finding":
        return f"Opened a finding: {f} ({POLICY.get(f, f)})"
    if op == "close_finding":
        return f"Closed the finding {f} ({POLICY.get(f, f)})"
    if op == "remember":
        return f"Remembered {f}: {short(a)}"
    if op == "forget":
        return f"Forgot {f}: {short(b)}"
    if op == "compact":
        return f"Compacted its {f}"
    if op == "keep_on_fetch_gap":
        return "Kept its memory through an empty fetch"
    return f"{op} {f}"


def overview() -> dict:
    with _cache_lock:
        if _ocache["data"] is not None and time.time() - _ocache["at"] < 1.5:
            return _ocache["data"]
    try:
        cards = RawTreeStore().latest_cards()
    except Exception as e:
        print(f"latest_cards failed: {type(e).__name__}")
        cards = {}
    site = _site_url()
    vendors, discarded, mem_tokens = [], 0, 0
    for v, c in sorted(cards.items()):
        subs = []
        for s in c.subprocessors:
            country, found, ev = s.country.strip(), False, s.url or ""
            if not country:
                inv = c.investigations.get(policy.sub_key(s), {})
                if inv.get("country"):
                    country, found, ev = inv["country"], True, inv.get("evidence_url") or ev
            subs.append({"name": s.name, "country": country, "found_by_agent": found, "evidence_url": ev})
        status = "not_compliant" if c.status == "red" else ("exposed" if c.exposed_via else "compliant")
        issues = [{"rule": f.rule, "title": _plain(f.summary), "policy": _policy(f.rule), "detail": _plain(f.summary)}
                  for f in c.open_findings]
        if not issues and c.exposed_via:
            issues = [{"rule": "", "title": "Depends on a vendor that is not compliant",
                       "policy": None, "detail": "Exposed via " + ", ".join(_name(x) for x in c.exposed_via)}]
        vendors.append({"slug": v.removesuffix("-mirror"), "name": _name(v), "status": status,
                        "subprocessor_count": len(c.subprocessors), "subprocessors": subs, "issues": issues})
        discarded += c.counters.get("noise", 0) + c.counters.get("unchanged", 0)
        mem_tokens += len(json.dumps(c.prompt_view())) // 4
    latest = None
    alerts = [a for a in _alerts() if not (a.get("title") or "").startswith("Baseline")]
    if alerts:
        a = alerts[0]
        vrow = _q(f"SELECT toString(vendor) v, toString(alert_id) i FROM {TABLES['alerts']} "
                  f"WHERE toString(tick) = '{a['tick']}' AND toString(rule) = '{a['rule']}'")
        vendor = vrow[0]["v"] if vrow else "tinybird-mirror"
        ep = next((e for e in _episodes() if e["tick"] == a["tick"] and e["vendor"] == vendor), None)
        agent = None
        if ep:
            steps = [_friendly_tool(s) for s in ep["steps"]]
            for loc in (ep["committed"] or {}).get("locations") or []:
                if isinstance(loc, dict):
                    steps.append({"text": f"Verified: {loc.get('name', '')} processes data in {loc.get('country', '')}",
                                  "detail": loc.get("evidence_url", "")})
                else:
                    m = re.match(r"(.+?):\s*(.+)", str(loc))
                    steps.append({"text": f"Verified: {m.group(1)} processes data in {m.group(2)}" if m else
                                  f"Verified: {loc}", "detail": ""})
            agent = {"model": ep["model"], "seconds": round(ep["latency_ms"] / 1000, 1), "steps": steps}
        edits = [_op_phrase(o) for o in _memory_ops() if o["tick"] == a["tick"] and o["vendor"] == vendor][:5][::-1]
        latest = {"kind": a["kind"], "vendor": _name(vendor), "title": _plain(a["title"]),
                  "headline": _headline(a["kind"], vendor), "why": _why(a), "policy": _policy(a["rule"]),
                  "evidence_url": a["evidence_url"], "tick": a["tick"], "agent": agent, "memory_edits": edits}
    naive: dict[str, tuple[int, int]] = {}  # latest naive-history size per vendor, summed
    for r in _q(f"SELECT toString(tick) t, toString(vendor) v, toString(input_tokens) i FROM {TABLES['ticks']} "
                f"WHERE toString(agent) = 'naive'"):
        if r["v"] not in naive or _int(r["t"]) >= naive[r["v"]][0]:
            naive[r["v"]] = (_int(r["t"]), _int(r["i"]))
    data = {"site_url": site, "suffix": SUFFIX, "watching": bool(cards), "vendors": vendors, "latest_event": latest,
            "stats": {"checks": max((c.tick for c in cards.values()), default=0), "discarded": discarded,
                      "memory_tokens": mem_tokens, "naive_tokens": sum(x[1] for x in naive.values()), "proof": _proof()}}
    with _cache_lock:
        _ocache.update(at=time.time(), data=data)
    return data


class _Job:
    def __init__(self, kind: str):
        self.d = {"id": uuid.uuid4().hex[:10], "kind": kind, "status": "running", "steps": [], "error": None}
        _jobs[self.d["id"]] = self.d

    def add(self, text: str, detail: str = "", state: str = "active") -> dict:
        s = {"text": text, "state": state, "detail": detail}
        self.d["steps"].append(s)
        return s

    def start(self, text: str, detail: str = "") -> dict:
        """Mark the previous active step done, then open a new active one."""
        for s in self.d["steps"]:
            if s["state"] == "active":
                s["state"] = "done"
        return self.add(text, detail)

    def finish(self, error: str | None = None) -> None:
        for s in self.d["steps"]:
            if s["state"] == "active":
                s["state"] = "failed" if error else "done"
        if error:
            self.add("Something went wrong", error, "failed")
        self.d.update(status="failed" if error else "done", error=error)
        with _cache_lock:
            _cache["data"] = None
            _ocache["data"] = None


def _launch(kind: str, fn, *args) -> tuple[int, dict]:
    if not _lock.acquire(blocking=False):
        return 409, {"error": "busy"}
    job = _Job(kind)

    def run():
        try:
            fn(job, *args)
            job.finish()
        except Exception as e:  # noqa: BLE001
            job.finish(f"{type(e).__name__}: {str(e)[:300]}")
        finally:
            _lock.release()
    threading.Thread(target=run, daemon=True).start()
    return 200, {"job": job.d["id"]}


def _onboard(job: _Job, url: str) -> None:
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from .. import ingest
    from ..contracts import VENDORS
    from . import run_tick
    url = url.strip().rstrip("/")
    if not re.match(r"^https?://", url):
        url = "https://" + url
    url = re.sub(r"/(subprocessors|privacy|terms)$", "", url)
    s = job.start("Reading your sub-processor list", url + "/subprocessors")
    html, via = ingest._read(url + "/subprocessors")
    slugs = [x for x in dict.fromkeys(re.findall(r"/vendors/([a-z0-9-]+)/", html)) if x in VENDORS]
    if not slugs:
        raise RuntimeError(f"no vendors found on {url}/subprocessors")
    s["detail"] = f"Found {len(slugs)} vendors: {', '.join(VENDORS[x] for x in slugs)}"
    s["state"] = "done"
    os.environ["SITE_URL"] = url
    steps = {v: job.add(f"Reading {VENDORS[v]}'s sub-processors and data terms") for v in slugs}
    snaps = []
    with ThreadPoolExecutor(len(slugs)) as pool:
        futs = {pool.submit(ingest.fetch_vendor, v): v for v in slugs}
        for f in as_completed(futs):
            v, snap = futs[f], f.result()
            if snap is None:
                steps[v].update(state="failed", detail="could not read this vendor's pages")
                continue
            snaps.append(snap)
            steps[v].update(state="done", detail=f"{len(snap.subprocessors)} sub-processors · via Nimble")
    if not snaps:
        raise RuntimeError("could not read any vendor's pages")
    rawtree.insert("snapshots", [x.to_row() for x in snaps])

    class Fresh(RawTreeStore):  # the snapshots just fetched: no RawTree read-after-write lag on fresh tables
        written: list = []
        ticks: list = []

        def latest_snapshots(self):
            return snaps

        def write(self, cards, alerts, ticks, ops=()):
            self.written, self.ticks = list(cards), list(ticks)
            super().write(cards, alerts, ticks, ops)

    s = job.start("Checking their terms against your Privacy Policy")
    store = Fresh()
    run_tick(store, run_id="live", agent=True)
    judged = sum(1 for c in store.written for v in c.sentence_verdicts.values()
                 if "liquid" in (v.get("by"), (v.get("first_opinion") or {}).get("by")))
    s["detail"] = (f"Liquid judged {judged} policy sentences" if judged else
                   f"Checked {len(store.written)} vendors' terms against your policy")
    s = job.start("Building memory")
    toks = sum(len(json.dumps(c.prompt_view())) // 4 for c in store.written)
    s["detail"] = f"{len(store.written)} state cards, {toks:,} tokens"
    want = {c.vendor for c in store.written}
    for _ in range(30):  # fresh tables: wait until the page will actually see the new memory
        try:
            if want <= set(RawTreeStore().latest_cards()):
                break
        except Exception:
            pass
        time.sleep(0.5)
    job.start(f"Watching {len(snaps)} vendors", url)


_LINE = re.compile(r"^\d\d:\d\d:\d\d\s+")


def _once(job: "_Job", text: str, detail: str) -> None:
    """The same kind of step twice in one run (e.g. Liquid judging again inside the episode): update, don't repeat."""
    prev = next((st for st in job.d["steps"] if st["text"] == text), None)
    if prev is None:
        job.start(text, detail)
    elif detail:
        prev["detail"] = detail


def _simulate(job: _Job, scenario: str) -> None:
    env = dict(os.environ, NO_COLOR="1", PYTHONUNBUFFERED="1", SITE_URL=_site_url())
    job.start("Applying the change to Tinybird's demo mirror")
    p = subprocess.run(SCENARIOS[scenario], cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
    if p.returncode != 0:
        raise RuntimeError(((p.stderr or p.stdout) or "inject failed").strip()[-300:])
    job.d["steps"][-1]["detail"] = ANSI.sub("", p.stdout).strip().splitlines()[-1][:200] if p.stdout.strip() else ""
    job.start("Reading Tinybird's pages")
    proc = subprocess.Popen(TICK, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            bufsize=1)
    result, tail = None, []
    t0 = time.time()
    for raw in proc.stdout:
        line = _LINE.sub("", ANSI.sub("", raw.rstrip("\n"))).strip()
        tail = (tail + [line])[-8:]
        if time.time() - t0 > 170:
            proc.kill()
            raise RuntimeError("the check took too long")
        if "(demo mirror)" in line and " via " in line and "companies" in line:
            m = re.search(r"(\d+) companies", line)
            job.d["steps"][-1].update(text="Read Tinybird's pages via Nimble",
                                      detail=f"{m.group(1) if m else '?'} sub-processors · via {line.split(' via ')[-1]}")
        elif "(demo mirror)" in line and "FAILED" in line:
            job.d["steps"][-1].update(detail="Nimble read failed: " + line.split("FAILED", 1)[-1].strip()[:160])
        elif line.startswith("agent: episode on") and "open questions:" in line:
            q = line.split("open questions:", 1)[1].strip()
            if q and q != "none":
                job.start(f"Found a sub-processor with no listed location: {q}")
                job.start("Investigating")
            else:
                job.start("Investigating the change")
        elif line.startswith("tool fetch_page("):
            m = re.match(r"tool fetch_page\((.*?)\) ->", line)
            from urllib.parse import urlparse
            u = m.group(1) if m else ""
            opened = job.start(f"Opened {urlparse(u).path or u} via Nimble", u)
            opened["_trunc"] = urlparse(u).path if len(u) >= 70 else ""
        elif line.startswith("tool recall("):
            _once(job, "Checked its memory of this vendor", "")
        elif line.startswith("tool search_web("):
            job.start("Searched the web", line[16:].split(") ->")[0][:120])
        elif line.startswith("commit ") and "verified on" in line:
            m = re.match(r"commit (.+?): (.+?), verified on (.*)", line)
            if m:
                for st in job.d["steps"]:  # narration cuts long URLs at 70 chars: restore the full path
                    tr = st.pop("_trunc", "")
                    if tr and m.group(3).strip().startswith(tr):
                        st["text"] = f"Opened {m.group(3).strip()} via Nimble"
                        st["detail"] = st.get("detail", "").split(tr)[0] + m.group(3).strip()
                job.start(f"Verified: {m.group(1)} processes data in {m.group(2)}", m.group(3))
        elif line.startswith("commit ") and "REJECTED" in line:
            job.start("Rejected a location it could not verify", line[7:])
        elif re.match(r"Liquid .* judged", line):
            _once(job, "Liquid read the changed policy sentence", line)
        elif re.match(r"\S+ (confirmed|overrode) ", line):
            q = re.search(r'"(.*)"', line)
            _once(job, "GPT-5.6 Sol double-checked it", q.group(1) if q else "")
        elif line.startswith("memory:"):
            job.start("Updated its memory", line[7:].strip()[:200])
        elif line.startswith(("VIOLATION", "resolved")):
            kind = "violation" if line.startswith("VIOLATION") else "resolved"
            parts = line.split(None, 2)
            title = _plain(parts[2]) if len(parts) > 2 else ""
            if result is None or kind == "violation":
                result = (kind, title)
        elif "runner: cycle" in line and "failed" in line:
            tail.append(line)
    rc = proc.wait()
    for st in job.d["steps"]:
        st.pop("_trunc", None)
    if rc != 0:
        raise RuntimeError("the check failed: " + " | ".join(tail[-3:])[-300:])
    if result:
        job.start("Result: " + _headline(result[0], "tinybird"), result[1])
    else:
        job.start("Result: nothing changed that affects your policies")


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj) -> None:
        self._send(code, json.dumps(obj, default=str).encode(), "application/json")

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/":
            self._send(200, _app_page().encode(), "text/html; charset=utf-8")
        elif path == "/guided":  # lane C's guided stage page
            self._send(200, _stage_page().encode(), "text/html; charset=utf-8")
        elif path == "/api/overview":
            self._json(200, overview())
        elif path.startswith("/api/job/"):
            j = _jobs.get(path.rsplit("/", 1)[-1])
            self._json(200 if j else 404, j or {"error": "no such job"})
        elif path == "/classic":  # lane B's original all-panels page, kept as a fallback
            self._send(200, PAGE.replace("__SUFFIX__", SUFFIX or "(stage tables)").encode(), "text/html; charset=utf-8")
        elif path == "/api/state":
            self._json(200, state())
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?")[0]
        if path not in ("/api/action", "/api/onboard", "/api/simulate"):
            return self._json(404, {"error": "not found"})
        try:
            n = min(int(self.headers.get("Content-Length") or 0), 4096)
            body = json.loads(self.rfile.read(n) or b"{}")
            name = body.get("action", "")
        except Exception:
            return self._json(400, {"ok": False, "seconds": 0, "output": "bad JSON", "error": "bad JSON"})
        if path == "/api/onboard":
            url = str(body.get("url") or "").strip()
            if not url or len(url) > 300 or not re.match(r"^(https?://)?[A-Za-z0-9.-]+(:\d+)?(/[\w./-]*)?$", url):
                return self._json(400, {"error": "enter a website address, like https://example.com"})
            return self._json(*_launch("onboard", _onboard, url))
        if path == "/api/simulate":
            sc = str(body.get("scenario") or "")
            if sc not in SCENARIOS:
                return self._json(400, {"error": f"unknown scenario (allowed: {', '.join(SCENARIOS)})"})
            return self._json(*_launch("simulate", _simulate, sc))
        code, body = run_action(str(name))
        self._json(code, body)

    def log_message(self, fmt, *args):
        if not any(x in (args[0] if args else "") for x in ("/api/state", "/api/overview", "/api/job/")):
            super().log_message(fmt, *args)


WEB_CONSOLE = Path(__file__).resolve().parents[3] / "web" / "console.html"


def _app_page() -> str:
    """The B2B app (src/sept25_build/agent/ui/app.html), re-read on every load."""
    try:
        return APP_PAGE.read_text().replace("__SUFFIX__", SUFFIX or "(stage tables)")
    except OSError:
        return ("<!doctype html><meta charset=utf-8><title>Night's Watch</title><body style='font-family:sans-serif;"
                "background:#0b1020;color:#e6ecff;padding:40px'><h1>Night's Watch</h1><p>The app page is loading. "
                "Meanwhile: <a style='color:#8ab4ff' href='/guided'>guided console</a> &middot; "
                "<a style='color:#8ab4ff' href='/classic'>classic console</a></p>")


def _stage_page() -> str:
    """Lane C's stage page (web/console.html), re-read on every load so design tweaks need no
    restart. Falls back to the classic page if it's missing."""
    try:
        page = WEB_CONSOLE.read_text()
    except OSError:
        page = PAGE
    return page.replace("__SUFFIX__", SUFFIX or "(stage tables)")


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Night's Watch Console</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{--bg:#070b16;--panel:#0e1526;--line:#1f2a44;--text:#e6ecff;--dim:#8b97b8;--green:#34d399;--red:#f87171;
--amber:#fbbf24;--blue:#60a5fa;--violet:#a78bfa}
*{box-sizing:border-box}html,body{margin:0;background:var(--bg);color:var(--text);
font:16px/1.35 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;height:100%}
header{display:flex;align-items:baseline;gap:18px;padding:10px 18px;border-bottom:1px solid var(--line)}
header h1{margin:0;font-size:26px}header .sfx{color:var(--amber);font-weight:600}header .at{color:var(--dim);margin-left:auto}
main{display:grid;grid-template-columns:1.25fr 1fr 1.2fr;gap:12px;padding:12px 18px;height:calc(100vh - 56px - 210px)}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:10px 12px;overflow:auto;min-height:0}
.panel h2{margin:0 0 8px;font-size:15px;text-transform:uppercase;letter-spacing:.08em;color:var(--dim)}
.col{display:flex;flex-direction:column;gap:12px;min-height:0}
.root{font-weight:700;font-size:18px;padding:6px 10px;border:1px solid var(--blue);border-radius:8px;display:inline-block;margin-bottom:8px}
.vend{border-left:4px solid var(--green);padding:6px 10px;margin:6px 0 6px 18px;background:#0b1222;border-radius:6px}
.vend.red{border-color:var(--red)}.vend.amber{border-color:var(--amber)}
.vend .name{font-weight:700;font-size:17px}.dot{display:inline-block;width:11px;height:11px;border-radius:50%;margin-right:6px;background:var(--green)}
.red .dot{background:var(--red)}.amber .dot{background:var(--amber)}
.meta{color:var(--dim);font-size:13px}.chips{margin-top:4px}
.chip{display:inline-block;font-size:12.5px;padding:2px 7px;margin:2px 3px 0 0;border-radius:10px;border:1px solid var(--line);color:#c7d2fe}
.chip.bad{border-color:var(--red);color:#fecaca;background:#3a1016}.chip.unk{border-color:var(--amber);color:#fde68a}
.badge{font-size:11px;padding:1px 5px;border-radius:6px;background:var(--violet);color:#1a1033;font-weight:700;margin-left:4px}
.finding{color:#fecaca;font-size:14px;margin-top:3px}
.btns{display:grid;grid-template-columns:1fr 1fr;gap:8px}
button{font:600 16px/1.2 inherit;padding:11px 10px;border-radius:8px;border:1px solid var(--line);background:#16213b;color:var(--text);cursor:pointer}
button:hover{border-color:var(--blue)}button:disabled{opacity:.45;cursor:wait}
button.big{grid-column:1/3;font-size:22px;padding:16px;background:#1d4ed8;border-color:#3b82f6}
button.proof{grid-column:1/3;background:#2e1065;border-color:var(--violet)}
#log{flex:1;min-height:0;margin:0;font:13px/1.35 ui-monospace,Menlo,monospace;white-space:pre-wrap;color:#cbd5e1;overflow:auto}
.spin{display:none;width:16px;height:16px;border:3px solid var(--line);border-top-color:var(--blue);border-radius:50%;animation:s 1s linear infinite;vertical-align:middle;margin-right:6px}
.busy .spin{display:inline-block}@keyframes s{to{transform:rotate(360deg)}}
.alert{border-left:4px solid var(--red);padding:5px 9px;margin-bottom:7px;background:#1a0f16;border-radius:5px}
.alert.resolved{border-color:var(--green);background:#0d1a16}.alert .t{font-weight:700;font-size:15px}
.alert .x{font-size:13.5px;color:#cbd5e1}.step{font-size:13px;margin:4px 0;padding:4px 7px;background:#0b1222;border-radius:5px}
.step b{color:var(--blue)}.mono{font-family:ui-monospace,Menlo,monospace;font-size:12px;color:var(--dim);word-break:break-all}
.op{font-size:13.5px;margin:3px 0}.opb{display:inline-block;min-width:74px;text-align:center;font-size:11px;font-weight:800;padding:2px 5px;border-radius:5px;margin-right:6px;background:#334155}
.op-remember,.op-open_finding{background:#7f1d1d}.op-forget,.op-close_finding{background:#065f46}.op-set{background:#1e3a8a}.op-compact{background:#4c1d95}
footer{display:grid;grid-template-columns:1.4fr 1fr;gap:12px;padding:0 18px 12px;height:210px}
.nums{display:grid;grid-template-columns:repeat(4,1fr);gap:8px}.num{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:10px}
.num .v{font-size:34px;font-weight:800;color:var(--green)}.num .l{font-size:13px;color:var(--dim)}
.empty{color:var(--dim);font-style:italic}
</style></head><body>
<header><h1>Night's Watch &mdash; live</h1><span>tables: <span class="sfx">__SUFFIX__</span></span><span class="at" id="at">loading...</span></header>
<main>
 <section class="panel" id="graph"><h2>Supply chain</h2><div id="g" class="empty">loading...</div></section>
 <section class="col">
  <div class="panel"><h2>Stage actions</h2><div class="btns">
   <button data-a="touch">1 Noise: date bump</button>
   <button data-a="dataharvest">2 Inject: DataHarvest (no country)</button>
   <button data-a="training">3 Inject: training clause</button>
   <button data-a="reset">4 Reset</button>
   <button data-a="tick" class="big">Run tick</button>
   <button data-a="proof" class="proof">Proof scorecard</button></div></div>
  <div class="panel col" id="logp" style="flex:1"><h2><span class="spin"></span><span id="logt">Narration</span></h2><pre id="log">Press a button. Output appears here.</pre></div>
 </section>
 <section class="col">
  <div class="panel" style="flex:1.1"><h2>Alerts</h2><div id="alerts"></div></div>
  <div class="panel" style="flex:1.3"><h2>Agent episode</h2><div id="ep"></div></div>
  <div class="panel" style="flex:1"><h2>Memory edits</h2><div id="ops"></div></div>
 </section>
</main>
<footer>
 <div class="panel"><h2>Tokens per tick: Night's Watch working memory vs naive history (computed, not run)</h2><div id="chart"></div></div>
 <div id="nums" class="nums"></div>
</footer>
<script>
const $=id=>document.getElementById(id);
const esc=s=>String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const cut=(s,n)=>{s=typeof s==='string'?s:JSON.stringify(s);return s&&s.length>n?s.slice(0,n)+'...':(s||'')};
const fmt=n=>Number(n||0).toLocaleString();
function graph(vs){
 if(!vs.length){$('g').innerHTML='<div class="empty">no state cards yet</div>';return}
 let h='<div class="root">Night\'s Watch Inc.</div>';
 for(const v of vs){
  const cls=v.status==='red'?'red':(v.exposed_via&&v.exposed_via.length?'amber':'');
  h+=`<div class="vend ${cls}"><div class="name"><span class="dot"></span>${esc(v.display_name)}</div>`;
  h+=`<div class="meta">tick ${v.tick} &middot; card ~${fmt(v.card_tokens)} tokens`;
  if(v.depends_on&&v.depends_on.length)h+=` &middot; depends on ${esc(v.depends_on.map(d=>d.replace('-mirror','').toUpperCase()).join(', '))}`;
  if(v.exposed_via&&v.exposed_via.length)h+=` &middot; <span style="color:var(--amber)">exposed via ${esc(v.exposed_via.join(', '))}</span>`;
  h+='</div><div class="chips">';
  for(const s of v.subprocessors){
   const c=s.country?(s.approved===false?'bad':''):'unk';
   h+=`<span class="chip ${c}">${esc(s.name)} &middot; ${esc(s.country||'country ?')}${s.found_by_agent?'<span class="badge">found by agent</span>':''}</span>`;
  }
  h+='</div>';
  for(const f of v.open_findings)h+=`<div class="finding">&#9888; ${esc(f.rule)}: ${esc(f.summary)}</div>`;
  h+='</div>';
 }
 $('g').innerHTML=h;
}
function alerts(a){
 $('alerts').innerHTML=a.length?a.map(x=>`<div class="alert ${x.kind==='resolved'?'resolved':''}"><div class="t">t${x.tick} ${esc(x.rule)} &middot; ${esc(x.title)}</div><div class="x">${esc(cut(x.explanation,260))}</div></div>`).join(''):'<div class="empty">no alerts</div>';
}
function episode(es){
 const e=es[0];if(!e){$('ep').innerHTML='<div class="empty">no episodes yet</div>';return}
 let h=`<div><b>t${e.tick} ${esc(e.vendor)}</b> &middot; trigger: <b>${esc(e.trigger)}</b></div><div class="meta">${esc(e.model)} &middot; ${fmt(e.latency_ms)} ms</div>`;
 for(const s of (e.steps||[]))h+=`<div class="step"><b>${esc(s.tool||'step')}</b> <span class="mono">${esc(cut(s.args,110))}</span><div class="mono">&rarr; ${esc(cut(s.result,170))}</div></div>`;
 const c=e.committed||{};
 h+=`<div class="step"><b>commit</b> ${esc((c.locations||[]).join('; ')||'nothing')}${(c.rejected||[]).length?' &middot; rejected: '+esc(cut(c.rejected,120)):''}${c.memo||c.note?'<div class="mono">memo: '+esc(cut(c.memo||c.note,220))+'</div>':''}</div>`;
 if(e.outcome)h+=`<div class="x" style="font-size:13.5px">${esc(cut(e.outcome,260))}</div>`;
 $('ep').innerHTML=h;
}
function ops(o){
 const lab={remember:'REMEMBER',forget:'FORGET',set:'SET',open_finding:'OPEN',close_finding:'CLOSE',compact:'COMPACT',keep_on_fetch_gap:'KEEP'};
 $('ops').innerHTML=o.length?o.map(x=>`<div class="op"><span class="opb op-${esc(x.op)}">${lab[x.op]||esc(x.op).toUpperCase()}</span>t${x.tick} ${esc(x.vendor)} <b>${esc(x.field)}</b> <span class="meta">${esc(cut(x.after||x.before,120))}${x.why?' &middot; '+esc(cut(x.why,80)):''}</span></div>`).join(''):'<div class="empty">no memory edits</div>';
}
function chart(s){
 const W=820,H=150,P=44,t=s.ticks||[];
 if(!t.length){$('chart').innerHTML='<div class="empty">no ticks yet</div>';return}
 const mx=Math.max(1,...s.naive,...s.nights_watch),x=i=>P+(t.length<2?0:i*(W-P-10)/(t.length-1)),y=v=>H-18-(v/mx)*(H-30);
 const line=(a,c)=>`<polyline fill="none" stroke="${c}" stroke-width="3" points="${a.map((v,i)=>x(i)+','+y(v)).join(' ')}"/>`+a.map((v,i)=>`<circle cx="${x(i)}" cy="${y(v)}" r="3.5" fill="${c}"/>`).join('');
 const last=t.length-1,left=x(last)<W/2,lx=left?x(last)+10:x(last)-6,anc=left?'start':'end';
 $('chart').innerHTML=`<svg viewBox="0 0 ${W} ${H}" width="100%" height="150">
  <line x1="${P}" y1="${H-18}" x2="${W}" y2="${H-18}" stroke="#1f2a44"/><text x="4" y="16" fill="#8b97b8" font-size="12">${fmt(mx)}</text><text x="4" y="${H-20}" fill="#8b97b8" font-size="12">0</text>
  ${t.map((v,i)=>`<text x="${x(i)}" y="${H-3}" fill="#8b97b8" font-size="11" text-anchor="middle">t${v}</text>`).join('')}
  ${line(s.naive,'#f87171')}${line(s.nights_watch,'#34d399')}
  <text x="${lx}" y="${Math.max(30,y(s.naive[last])-8)}" fill="#f87171" font-size="13" text-anchor="${anc}">naive ${fmt(s.naive[last])} (computed, not run)</text>
  <text x="${lx}" y="${y(s.nights_watch[last])+18}" fill="#34d399" font-size="13" text-anchor="${anc}">Night's Watch ${fmt(s.nights_watch[last])}</text></svg>`;
}
function nums(p){
 if(!p||!p.long_horizon){$('nums').innerHTML='<div class="num"><div class="l">Run "Proof scorecard" for the 365-day numbers</div></div>';return}
 const L=p.long_horizon,C=p.crash_test||{};
 const n=[[L.ratio_end+'x','smaller context on day '+L.days+' ('+fmt(L.cards_tokens_end)+' vs '+fmt(L.naive_tokens_end)+' tokens)'],
  ['day '+L.naive_over_window_day,'naive history passes the '+fmt(L.window_tokens)+'-token window'],
  [L.matched+'/'+L.truth,'alerts matched ground truth ('+L.missed+' missed, '+L.unexpected+' unexpected)'],
  [(C.recoveries!=null?C.recoveries+'/'+C.crashes:'?'),'crash recoveries, '+(C.cards_identical?'cards identical':'cards differ')]];
 $('nums').innerHTML=n.map(([v,l])=>`<div class="num"><div class="v">${esc(v)}</div><div class="l">${esc(l)}</div></div>`).join('');
}
let running=false;
async function poll(){
 try{const r=await fetch('/api/state');const d=await r.json();
  graph(d.vendors||[]);alerts(d.alerts||[]);episode(d.episodes||[]);ops(d.memory_ops||[]);chart(d.series||{});nums(d.proof);
  $('at').textContent='refreshed '+d.at;
 }catch(e){$('at').textContent='refresh failed '+new Date().toLocaleTimeString()}
}
async function act(a,label){
 if(running)return;running=true;document.querySelectorAll('button').forEach(b=>b.disabled=true);
 $('logp').classList.add('busy');$('logt').textContent=label+' ...';$('log').textContent='';
 try{const r=await fetch('/api/action',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:a})});
  const d=await r.json();$('logt').textContent=`${label}: ${d.ok?'done':'FAILED'} in ${d.seconds}s`;$('log').textContent=d.output||'(no output)';
 }catch(e){$('logt').textContent=label+': request failed';$('log').textContent=String(e)}
 $('log').scrollTop=$('log').scrollHeight;$('logp').classList.remove('busy');
 document.querySelectorAll('button').forEach(b=>b.disabled=false);running=false;poll();
}
document.querySelectorAll('button[data-a]').forEach(b=>b.onclick=()=>act(b.dataset.a,b.textContent));
poll();setInterval(poll,2000);
</script></body></html>"""


def main() -> None:
    ap = argparse.ArgumentParser(description="Night's Watch stage console")
    ap.add_argument("--port", type=int, default=8790)
    a = ap.parse_args()
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    print(f"Night's Watch console on http://127.0.0.1:{a.port}  tables suffix: {SUFFIX or '(none: stage tables)'}",
          flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
