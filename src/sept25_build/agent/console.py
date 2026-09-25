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
                if "agent followed" in ex:
                    v = re.search(r"\.\s*([^.]+?)(?: \(demo mirror\))? lists no location", ex)
                    who = v.group(1).strip() if v else "The vendor"
                    return s + f". {who} doesn't list a location; Night's Watch found it on {m.group(1)}'s own website."
                return s + "."
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


def _excerpt(res: str) -> str:
    """One readable line from a tool result: prefer a line naming a location, strip markdown and tags."""
    text = re.sub(r"^\[[^\]]*\]\s*", "", res)                      # "[via Nimble] "
    text = re.sub(r"[*#_`>]+", " ", text)
    parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+|\s{2,}|\n", text) if p.strip()]
    hit = next((p for p in parts if re.search(r"location|headquarter|processing", p, re.I)), None)
    return (hit or (parts[0] if parts else ""))[:140]


def _friendly_tool(s: dict) -> dict:
    tool, args, res = s.get("tool", ""), s.get("args") or {}, str(s.get("result") or "")
    if tool in ("fetch_page", "search_web"):
        res = _excerpt(res)
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


def app_overview() -> dict:
    """What the app page (agent/ui/app.html) shows: the site the user checked last."""
    with _real_lock:
        if _real.get("vendors"):
            return _real_overview()
    return {"site_url": "", "real": True, "watching": False, "suffix": SUFFIX, "vendors": [], "latest_event": None,
            "stats": {}}




def overview() -> dict:
    """The recorded RawTree run (stage demo tables); web/export_demo.py and the stage pages read this."""
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
            job.finish(str(e)[:400] if type(e) is RuntimeError else f"{type(e).__name__}: {str(e)[:300]}")
        finally:
            _lock.release()
    threading.Thread(target=run, daemon=True).start()
    return 200, {"job": job.d["id"]}


_SUBP = re.compile(r"sub[\s_-]?processors?", re.I)
_HINT = re.compile(r"trust|legal|privacy|dpa|gdpr|security|compliance", re.I)
_ALINK = re.compile(r"""<a\b[^>]*?href=["']([^"'#]+)["'][^>]*>(.*?)</a>""", re.I | re.S)
_ARCHIVE = re.compile(r"archive|preview|/\d{4}-\d{2}-\d{2}|-20\d{6}", re.I)
_SKIP_URL = re.compile(r"log-?in|sign-?in|sign-?up|contact-sales|pricing|[?&](next|redirect|return)", re.I)
_LOCALE = re.compile(r"^/(?:intl/)?(?!en(?:[-_][a-z]{2})?/)[a-z]{2}(?:[-_][a-zA-Z]{2})?/")
_FILE_HOSTS = ("amazonaws.com", "cloudfront.net", "googleusercontent.com", "googleapis.com", "windows.net", "github.io",
               "githubusercontent.com", "notion.site", "sharepoint.com", "dropbox.com", "box.com")
_OK_PLACES = {"usa", "us", "u.s.", "u.s", "u.s.a.", "united states of america", "united state of america", "america", "uk",
              "u.k.", "great britain", "england", "scotland", "wales", "northern ireland", "united kingdom of great britain",
              "eu", "eea", "european union", "europe", "european economic area", "czechia", "republic of ireland",
              "the netherlands", "holland", "federal republic of germany", "french republic", "kingdom of the netherlands",
              "republic of poland", "swiss confederation"}
_US_STATES = {"alabama", "alaska", "arizona", "arkansas", "california", "colorado", "connecticut", "delaware", "florida",
              "georgia", "hawaii", "idaho", "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana", "maine",
              "maryland", "massachusetts", "michigan", "minnesota", "mississippi", "missouri", "montana", "nebraska",
              "nevada", "new hampshire", "new jersey", "new mexico", "new york", "north carolina", "north dakota", "ohio",
              "oklahoma", "oregon", "pennsylvania", "rhode island", "south carolina", "south dakota", "tennessee", "texas",
              "utah", "vermont", "virginia", "washington", "west virginia", "wisconsin", "wyoming", "district of columbia"}
_VAGUE = {"global", "globally", "worldwide", "various", "multiple", "international", "emea", "apac", "amer", "latam",
          "asia pacific", "americas", "rest of world", "all regions", "multiple regions", "multiple countries", "local"}
_FILLER = re.compile(r"\b(?:hosted|hosting|in|the|servers?|located|locations?|of|processing|data|cent(?:er|re)s?|regions?|"
                     r"aws|azure|gcp|primary|secondary|backup|only|with|for|east|west|central|us-east-\d|eu-west-\d|n/a|"
                     r"na|none|see|below|above|note|may|also|entity|headquarters|hq|offices?|support)\b")
_ADDRESS = re.compile(r"\s+\d{1,5}\s+(?:[A-Z][\w.'-]*\s+){1,4}(?:St|Street|Ave|Avenue|Road|Rd|Blvd|Boulevard|Way|Drive|Dr|"
                      r"Lane|Ln|Place|Pl|Square|Sq|Suite|Floor|Parkway|Pkwy|Court|Ct)\b.*$")
_NAME_KEYS = ("sub-processor", "subprocessor", "name", "company", "vendor", "provider", "processor", "entity", "third part")
_LOC_KEYS = ("processing location", "location of processing", "data location", "hosting location", "country of processing",
             "entity country", "country", "countries", "location", "jurisdiction", "region", "where")
_PURPOSE_KEYS = ("processing activit", "purpose", "nature", "function", "description", "activit", "service")


def _text(h: str) -> str:
    import html as _html
    t = _html.unescape(re.sub(r"<[^>]+>", " ", h)).replace("\\n", " ").replace("\\t", " ").replace('\\"', '"')
    return re.sub(r"\s+", " ", t).strip()


def _fetch(u: str, render: bool = False) -> str:
    """Page HTML via Nimble (direct GET if Nimble errors); '' on any failure or HTTP error."""
    from .. import ingest
    try:
        r = ingest._nimble().extract.run(url=u, formats=["html"], render=render)
        return (r.data.html or "") if (r.status_code or 200) < 400 else ""
    except Exception:
        try:
            import requests
            resp = requests.get(u, timeout=8, headers={"User-Agent": "Mozilla/5.0 NightsWatch"})
            return resp.text if resp.status_code < 400 else ""
        except Exception:
            return ""


def _on_site(h: str, site: str) -> bool:
    return h == site or h.endswith("." + site)


def _links(page: str, base: str, site: str) -> list[tuple[int, str]]:
    import html as _html
    from urllib.parse import urljoin, urlparse
    out = []
    for href, label in _ALINK.findall(page):
        href = _html.unescape(href.strip())
        u = urljoin(base, href).split("#")[0]
        p = urlparse(u)
        if not u.startswith("http") or _SKIP_URL.search(u) or _LOCALE.match(p.path or "/"):
            continue
        both = href + " " + _text(label)
        h = (p.hostname or "").lower()
        if _SUBP.search(both) and _on_site(h, site):
            out.append((3, u))
        elif _HINT.search(both) and (_on_site(h, site) or h.startswith("trust.")):
            out.append((1, u))
    return out


def _pick(head: list[str], keys: tuple, skip=()) -> int | None:
    for k in keys:  # keyword priority first, so "Entity Country" beats a coarse "Region" column to its left
        for i, x in enumerate(head):
            if i not in skip and k in x:
                return i
    return None


def _clean_name(name: str) -> str:
    name = _ADDRESS.sub("", name)
    name = re.sub(r"\(\s*effective\b.*$", "", name, flags=re.I)
    return name.strip(" ,*†‡")


def _rows(page: str) -> list[dict]:
    rows = []
    for table in re.findall(r"<table\b.*?</table>", page, re.I | re.S):
        cells = [[_text(c) for c in re.findall(r"<t[hd]\b[^>]*>(.*?)</t[hd]>", tr, re.I | re.S)]
                 for tr in re.findall(r"<tr\b.*?</tr>", table, re.I | re.S)]
        cells = [c for c in cells if any(c)]
        hi = next((i for i, c in enumerate(cells[:3]) if len(c) >= 2 and _pick([x.lower() for x in c], _LOC_KEYS) is not None),
                  None)  # the header row: skips a title row spanning the table
        if hi is None:
            continue
        head = [x.lower() for x in cells[hi]]
        ni = _pick(head, _NAME_KEYS)
        ni = 0 if ni is None else ni
        ci = _pick(head, _LOC_KEYS, skip=(ni,))
        if ci is None:  # a sub-processor table says where data goes; tables without a location column are something else
            continue
        pi = _pick(head, _PURPOSE_KEYS, skip=(ni, ci))
        for c in cells[hi + 1:]:
            c = [x[len(head[i]):].strip(" :") if i < len(head) and head[i] and x.lower().startswith(head[i]) and
                 len(x) > len(head[i]) else x for i, x in enumerate(c)]  # responsive tables repeat the column label
            if sum(1 for x in c if x) < 2:  # a section heading spanning the row, not a company
                continue
            if len(c) == len(head) - 1 and ni == 0 and rows:  # rowspan on the name: another location for the row above
                extra = c[ci - 1] if ci - 1 < len(c) else ""
                if extra and extra not in rows[-1]["country"]:
                    rows[-1]["country"] = (rows[-1]["country"] + "; " + extra)[:400]
                continue

            def g(i):
                return c[i] if i is not None and i < len(c) else ""
            name = _clean_name(g(ni))
            if name and len(name) <= 120 and name.lower() not in head:
                rows.append({"name": name, "purpose": g(pi)[:200], "country": g(ci)[:400]})
    return rows or _json_rows(page)


def _json_rows(page: str) -> list[dict]:
    """Trust portals (Wolfia, etc.) embed the list as JSON: "subprocessors":[{name, service_description, location}]."""
    import html as _html
    dec, text, rows = json.JSONDecoder(), _html.unescape(page), []
    for m in re.finditer(r'"sub_?processors"\s*:\s*\[', text, re.I):
        try:
            arr, _ = dec.raw_decode(text, m.end() - 1)
        except ValueError:
            continue
        for x in arr if isinstance(arr, list) else []:
            if not isinstance(x, dict):
                continue
            co = x.get("company") if isinstance(x.get("company"), dict) else {}
            name = x.get("display_name") or x.get("name") or co.get("name") or ""
            where = x.get("location") or x.get("country") or x.get("countries") or x.get("region") or ""
            where = ", ".join(where) if isinstance(where, list) else str(where)
            if name and isinstance(name, str):
                rows.append({"name": _clean_name(name)[:120], "country": where[:400],
                             "domain": str(co.get("domain") or x.get("domain") or "")[:100],
                             "purpose": str(x.get("service_description") or x.get("purpose") or x.get("description") or "")[:200]})
        if rows:
            break
    return rows


def _merge(rows: list[dict]) -> list[dict]:
    """One row per company; a company listed per product or region keeps every location (none silently dropped)."""
    out: dict[str, dict] = {}
    for r in rows:
        k = re.sub(r"[^a-z0-9]+", " ", r["name"].lower()).strip()
        if not k:
            continue
        if k not in out:
            out[k] = dict(r)
            continue
        o = out[k]
        for f in ("country", "purpose"):
            if r.get(f) and r[f] not in o.get(f, ""):
                o[f] = (o.get(f, "") + "; " + r[f]).strip("; ")[:400]
        o["domain"] = o.get("domain") or r.get("domain", "")
    return list(out.values())


def _review(loc: str) -> list[str]:
    """Places in a location cell that aren't on the approved list, plus values too vague to check (e.g. "Global")."""
    from ..contracts import APPROVED_COUNTRIES
    ok = sorted({c.lower() for c in APPROVED_COUNTRIES} | _OK_PLACES | _US_STATES, key=len, reverse=True)
    rest = re.sub(r"\(.*?\)", " ", (loc or "").replace("\\n", " ").lower())
    for place in ok:  # strip every approved place first, so "United States Australia Japan" leaves just "australia"
        rest = re.sub(r"(?<![a-z])" + re.escape(place) + r"(?![a-z])", ",", rest)
    named = rest.count(",") > (loc or "").count(",")  # the cell named at least one approved place
    out, unknown = [], []
    for p in re.split(r",|/|;|\n|&|\||\+|\band\b|\bor\b|:", rest):
        p = re.sub(r"\s+", " ", _FILLER.sub(" ", p)).strip(" .*-'\"")
        if not p or not re.search(r"[a-z]{2}", p):
            continue
        hits = [c for c in _OTHER_COUNTRIES if re.search(r"(?<![a-z])" + re.escape(c.lower()) + r"(?![a-z])", p)]
        hits = [c for c in hits if not any(c != o and c in o for o in hits)]  # "Korea" inside "South Korea"
        vague = [w.title() for w in _VAGUE if re.search(rf"(?<![a-z]){w}(?![a-z])", p)]
        if hits or vague:
            out += hits + vague
        elif len(p) <= 30:  # an unrecognised word: a city or state next to a named country, or an unknown place
            unknown.append(p.title() if len(p) > 4 else p.upper())
    if not out and not named:  # nothing recognised at all: surface what's there so a person checks it
        out = unknown
    if any(x.lower() not in _VAGUE for x in out):  # "Singapore (Asia Pacific)": the country is the finding
        out = [x for x in out if x.lower() not in _VAGUE]
    return list(dict.fromkeys(out))


def _issue_title(review: list[str]) -> str:
    places = [p for p in review if p.lower() not in _VAGUE]
    vague = [p for p in review if p.lower() in _VAGUE]
    return ("Processes data outside your approved countries: " + ", ".join(places) if places else
            "Location too vague to confirm: " + ", ".join(vague))


_REAL_FILE = ROOT / "out" / "nw_real_site.json"
_real_lock = threading.RLock()
try:
    _real: dict = json.loads(_REAL_FILE.read_text()) if _REAL_FILE.exists() else {}
    for _v in _real.get("vendors", []):
        if _v.get("chain") == "loading":  # the console stopped mid-read: let the next open retry
            _v["chain"] = "idle"
except Exception:
    _real = {}
_STOP = {"inc", "llc", "ltd", "limited", "corp", "corporation", "co", "company", "gmbh", "the", "and", "dba", "plc", "sa",
         "sas", "bv", "ag", "pte", "pty", "technologies", "technology", "services", "software", "group", "holdings", "labs",
         "cloud", "platform", "systems", "international", "global", "usa", "europe", "web", "data", "functional"}


def _save_real() -> None:
    with _real_lock:
        try:
            _REAL_FILE.parent.mkdir(exist_ok=True)
            _REAL_FILE.write_text(json.dumps(_real))
        except Exception as e:
            print(f"saving real-site state failed: {type(e).__name__}")
    with _cache_lock:
        _ocache["data"] = None


def _tokens(name: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9]+", name.lower()) if len(t) >= 3 and t not in _STOP]


def _owned(h: str, dom: str, toks: list[str]) -> bool:
    """Is host h the vendor's own site (not a third party that mentions it, not someone's file bucket)?"""
    if any(_on_site(h, f) for f in _FILE_HOSTS):
        return False
    if dom and _on_site(h, dom.split("/")[0]):
        return True
    labels = h.split(".")
    reg = labels[-3] if len(labels) >= 3 and labels[-2] in ("co", "com", "org", "net", "ac", "gov") else labels[-2] if len(labels) >= 2 else h
    return any(reg == t or reg.startswith(t) for t in toks)


def _chain(slug: str) -> None:
    """One level down: read this vendor's own sub-processor list (search its own domain via Nimble)."""
    from concurrent.futures import ThreadPoolExecutor
    from urllib.parse import urlparse
    with _real_lock:
        v = next((x for x in _real.get("vendors", []) if x["slug"] == slug), None)
    if v is None:
        return
    try:
        from .. import ingest
        toks = _tokens(v["name"]) or re.findall(r"[a-z0-9]+", v["name"].lower())[:1]
        dom = (v.get("domain") or "").lower().removeprefix("www.")
        q = " ".join(toks[:3]) or v["name"]  # "Snowflake, Inc." searches better as "snowflake"
        res = ingest._nimble().search(query=f"{q} subprocessors list", max_results=8, search_depth="lite")
        d = res.model_dump() if hasattr(res, "model_dump") else res
        cands = []
        for x in (d.get("results") or []):
            u = x.get("url") or ""
            h = (urlparse(u).hostname or "").lower()
            if _owned(h, dom, toks) and not _SKIP_URL.search(u) and (_SUBP.search(u) or _SUBP.search(x.get("title") or "")):
                cands.append(u)
        cands = sorted(dict.fromkeys(cands), key=lambda u: bool(_ARCHIVE.search(u)))[:4]
        best, rows = "", []
        if cands:
            with ThreadPoolExecutor(4) as pool:
                pages = dict(zip(cands, pool.map(_fetch, cands)))
            scored = sorted(cands, key=lambda u: (bool(_ARCHIVE.search(u)), -len(_rows(pages[u]))))
            if _rows(pages[scored[0]]):
                best, rows = scored[0], _rows(pages[scored[0]])
            else:
                for u in cands[:2]:
                    pg = _fetch(u, render=True)
                    if _rows(pg):
                        best, rows = u, _rows(pg)
                        break
        rows = _merge(rows)
        for r in rows:
            r["review"] = _review(r["country"]) if r["country"] else []
        with _real_lock:
            if not rows:
                v.update(chain="none", chain_error=f"No published sub-processor list found for {v['name']}")
            else:
                bad = sorted({p for r in rows for p in r["review"] if p.lower() not in _VAGUE})
                sim = [x for x in v.get("subprocessors", []) if x.get("simulated")]
                v.update(chain="done", chain_page=best, subprocessor_count=len(rows) + len(sim),
                         subprocessors=[{"name": r["name"], "country": r["country"], "review": r["review"],
                                         "purpose": r["purpose"]} for r in rows[:150]] + sim)
                if bad and v["status"] == "compliant":
                    v["status"] = "exposed"
                    v["issues"] = v["issues"] + [{"rule": "R1", "title": "Its own sub-processors process data outside your approved countries",
                                                  "detail": ", ".join(bad[:8]), "policy": None}]
            _save_real()
    except Exception as e:
        with _real_lock:
            v.update(chain="failed", chain_error=f"{type(e).__name__}: {str(e)[:160]}")
            _save_real()


def _start_chain(slug: str) -> tuple[int, dict]:
    with _real_lock:
        v = next((x for x in _real.get("vendors", []) if x["slug"] == slug), None)
        if v is None:
            return 404, {"error": "no such vendor"}
        if v["chain"] in ("loading", "done", "none"):
            return 200, {"chain": v["chain"]}
        v.update(chain="loading", chain_error="")
    with _cache_lock:
        _ocache["data"] = None
    threading.Thread(target=_chain, args=(slug,), daemon=True).start()
    return 200, {"chain": "loading"}


def _real_overview() -> dict:
    with _real_lock:
        r = json.loads(json.dumps(_real))
    vs = r.get("vendors", [])
    return {"site_url": r.get("site_url", ""), "real": True, "watching": True, "suffix": SUFFIX,
            "source_page": r.get("source_page", ""), "checked_at": r.get("checked_at"), "vendors": vs,
            "latest_event": r.get("latest_event"), "stats": {"checks": r.get("checks", 1)}}


def _discover(job: _Job, url: str) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from urllib.parse import urlparse
    from ..contracts import VENDOR_ALIASES
    host = (urlparse(url).hostname or "").lower()
    site = ".".join(host.split(".")[-2:])
    s = job.start(f"Looking for {host}'s sub-processor list", url)
    home = _fetch(url)
    ranked = sorted(set(_links(home, url, site)), key=lambda x: -x[0]) if home else []
    guesses = [url + p for p in ("/subprocessors", "/sub-processors", "/legal/subprocessors", "/legal/sub-processors",
                                 "/trust", "/legal", "/privacy")]
    found = []
    try:  # web search first: many companies keep the list on a trust subdomain that the homepage doesn't link to
        from .. import ingest
        res = ingest._nimble().search(query=f"{site} subprocessors list", max_results=8, search_depth="lite")
        d = res.model_dump() if hasattr(res, "model_dump") else res
        for x in (d.get("results") or []):
            u, h = x.get("url") or "", (urlparse(x.get("url") or "").hostname or "").lower()
            if _on_site(h, site) and not _SKIP_URL.search(u) and (_SUBP.search(u) or _SUBP.search(x.get("title") or "")):
                found.append(u)
        s["detail"] = f"Web search via Nimble: {len(found)} likely pages"
    except Exception:
        pass
    todo = list(dict.fromkeys(found + [u for _, u in ranked] + guesses))[:14]
    seen, pages = set(), {}

    def grab(batch):
        batch = list(dict.fromkeys(u for u in batch if u not in seen))[:12]
        seen.update(batch)
        with ThreadPoolExecutor(8) as pool:
            for u, pg in zip(batch, pool.map(_fetch, batch)):
                if pg:
                    pages[u] = pg
        s["detail"] = f"Checked {len(seen)} pages on {site} via Nimble"
    grab(todo)
    deeper = [u for pg_url, pg in list(pages.items()) for sc, u in _links(pg, pg_url, site) if sc == 3]
    grab(deeper)  # one level down: trust / legal pages usually link to the sub-processor list

    def score(u):
        n = len(_rows(pages[u])) if _SUBP.search(pages[u]) else 0
        return (n > 0, not _ARCHIVE.search(u), n, bool(_SUBP.search(u)))  # the live list beats an archived or preview one
    best = max(pages, key=score, default=None)
    rows = _rows(pages[best]) if best and score(best)[0] else []
    if not rows:  # JS-rendered trust centers: render the most likely page and try again
        cands = [u for u in pages if _SUBP.search(u) or _SUBP.search(pages[u])]
        for u in sorted(cands, key=lambda u: (u not in found, not _SUBP.search(u)))[:3]:
            s["detail"] = f"Rendering {u} via Nimble"
            pg = _fetch(u, render=True)
            if _rows(pg):
                best, rows = u, _rows(pg)
                break
    if not rows:
        raise RuntimeError(f"Couldn't find a readable sub-processor list on {host} (checked {len(seen)} pages). "
                           "Try the address of the page that lists them.")
    rows = _merge(rows)
    s["detail"] = f"Found it: {best}"
    s = job.start("Reading the sub-processors", best)
    for r in rows:
        r["review"] = _review(r["country"]) if r["country"] else []
        low = r["name"].lower()
        r["watched"] = next((v for v, al in VENDOR_ALIASES.items() if any(a in low for a in al)), "")
    s["detail"] = f"{len(rows)} sub-processors" + (f", {sum(1 for r in rows if r['watched'])} already watched by Night's Watch" if any(r["watched"] for r in rows) else "")
    s = job.start("Checking locations against your approved countries")
    bad = [r for r in rows if r["review"]]
    nol = [r for r in rows if not r["country"]]
    s["detail"] = (f"{len(bad)} need review" if bad else "All listed locations are approved") + (f"; {len(nol)} list no location" if nol else "")
    vendors = []
    for i, r in enumerate(rows[:120]):
        status = "review" if r["review"] else ("compliant" if r["country"] else "unknown")
        issues = [{"rule": "R1", "title": _issue_title(r["review"]),
                   "detail": f"Listed location: {r['country']}", "policy": None}] if r["review"] else []
        vendors.append({"slug": f"s{i}", "name": r["name"], "status": status, "own_status": status,
                        "location": r["country"], "purpose": r["purpose"], "domain": r.get("domain", ""),
                        "watched": r["watched"], "issues": issues, "subprocessor_count": None, "subprocessors": [],
                        "chain": "idle", "chain_page": "", "chain_error": ""})
    with _real_lock:
        _real.clear()
        _real.update(site_url=url, host=host, source_page=best, vendors=vendors, checks=1,
                     checked_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"))
        _save_real()


def _onboard(job: _Job, url: str) -> None:
    url = url.strip().rstrip("/")
    if not re.match(r"^https?://", url):
        url = "https://" + url
    _discover(job, url)


_LINE = re.compile(r"^\d\d:\d\d:\d\d\s+")


def _once(job: "_Job", text: str, detail: str) -> None:
    """The same kind of step twice in one run (e.g. Liquid judging again inside the episode): update, don't repeat."""
    prev = next((st for st in job.d["steps"] if st["text"] == text), None)
    if prev is None:
        job.start(text, detail)
    elif detail:
        prev["detail"] = detail


_OTHER_COUNTRIES = ["Singapore", "India", "China", "Hong Kong", "Russia", "Brazil", "Mexico", "Australia", "New Zealand",
                    "South Korea", "Israel", "Turkey", "United Arab Emirates", "Saudi Arabia", "South Africa", "Philippines",
                    "Vietnam", "Indonesia", "Malaysia", "Thailand", "Taiwan", "Argentina", "Chile", "Colombia", "Nigeria",
                    "Egypt", "Pakistan", "Ukraine", "Belarus", "Kazakhstan", "Korea", "Uruguay", "Paraguay", "Peru",
                    "Bolivia", "Ecuador", "Venezuela", "Panama", "Costa Rica", "Honduras", "Guatemala", "El Salvador",
                    "Dominican Republic", "Jamaica", "Kenya", "Ghana", "Morocco", "Tunisia", "Sri Lanka", "Bangladesh",
                    "Nepal", "Serbia", "Bosnia", "North Macedonia", "Albania", "Armenia", "Qatar", "Bahrain", "Kuwait",
                    "Oman", "Jordan", "Lebanon", "Uzbekistan", "Mongolia", "Cambodia", "Myanmar", "Macau"]
SIM_SCENARIOS = ("new_subprocessor", "moves_country", "custom", "undo")


def _find_location(page: str) -> tuple[str, str]:
    """(where the company processes data, how it was found) from its own web page; ("", "") if not found."""
    from ..contracts import APPROVED_COUNTRIES
    text = _text(page)[:12000]
    known = sorted(list(APPROVED_COUNTRIES) + _OTHER_COUNTRIES + ["European Union", "USA", "UK"], key=len, reverse=True)
    m = re.search(r"(?:data processing locations?|(?:process|store|host)(?:es|s)? (?:all |customer |personal )*data "
                  r"(?:only )?in)\s*[:\-]?\s*(.{1,120})", text, re.I)
    if m:  # the countries named right after the statement, in the order they appear
        after = m.group(1)
        hits = {c: after.find(c) for c in known if re.search(r"(?<![A-Za-z])" + re.escape(c) + r"(?![A-Za-z])", after)}
        hits = {c: i for c, i in hits.items() if not any(c != o and c in o for o in hits)}  # "UK" inside "United Kingdom"
        if hits:
            return ", ".join(sorted(hits, key=hits.get)), "stated on the page"
    try:
        from . import llm
        if llm.available():
            schema = {"type": "object", "additionalProperties": False, "required": ["country"],
                      "properties": {"country": {"type": "string"}}}
            out, _ = llm.json_call("You read a company's web page and answer where it processes customer data. "
                                   "Reply with the country or countries named on the page, or an empty string.",
                                   text[:6000], schema, max_tokens=60)
            c = str(out.get("country") or "").strip()
            if c and c.lower() in text.lower():
                return c, f"read by {llm.MODEL}"
    except Exception:
        pass
    counts = {c: len(re.findall(r"(?<![A-Za-z])" + re.escape(c) + r"(?![A-Za-z])", text))
              for c in list(APPROVED_COUNTRIES) + _OTHER_COUNTRIES}
    best = max(counts, key=counts.get)
    return (best, "most-mentioned country on the page") if counts[best] else ("", "")


def _real_simulate(job: _Job, scenario: str, slug: str, name: str = "", url: str = "") -> None:
    """Simulate a vendor change on Night's Watch's copy of the watched site's list; real pages are never touched."""
    t0 = time.time()
    with _real_lock:
        if not _real.get("vendors"):
            raise RuntimeError("Check a website first")
        if scenario == "undo":
            s = job.start("Undoing simulated changes")
            if _real.get("baseline"):
                _real["vendors"] = _real.pop("baseline")
            _real["latest_event"] = None
            _save_real()
            s["detail"] = "Back to the list as read from " + (_real.get("source_page") or "the site")
            return
        v = next((x for x in _real["vendors"] if x["slug"] == slug), None)
        if v is None:
            raise RuntimeError("Pick a vendor to change")
        _real.setdefault("baseline", json.loads(json.dumps(_real["vendors"])))
        vname = v["name"]
    steps: list[dict] = []

    def step(text, detail=""):
        st = job.start(text, detail)
        steps.append(st)
        return st
    if scenario == "moves_country":
        old, new = v.get("location") or "no listed location", "India"
        step(f"Simulating: {vname} moves customer data to {new}",
             f"Changed Night's Watch's copy of this list, not {vname}'s real pages")
        step("Reading the change", f"Location changed from {old} to {new}")
        step("Checking against your Privacy Policy", f"{new} is not an approved country")
        bad, evidence = True, _real.get("source_page", "")
        why = f"It now processes customer data in {new}, which is not an approved country."
        with _real_lock:
            v.update(location=new, status="not_compliant",
                     issues=[{"rule": "R1", "title": f"Processes data in {new}, which is not an approved country",
                              "detail": f"Was: {old}", "policy": None}])
    else:
        if scenario == "new_subprocessor":
            name, url = "DataHarvest Ltd", _site_url() + "/companies/dataharvest"
        if not name or not url.startswith("http"):
            raise RuntimeError("Enter the company's name and website")
        step(f"Simulating: {vname} adds {name} to its sub-processor list",
             f"Changed Night's Watch's copy of this list, not {vname}'s real pages")
        step(f"Found a new sub-processor with no listed location: {name}")
        st = step(f"Investigating {name}", url)
        page = _fetch(url)
        st["text"] = f"Opened {name}'s website via Nimble" if page else f"Couldn't open {name}'s website"
        country, how = _find_location(page) if page else ("", "")
        step("Finding where it processes data", f"{country} ({how})" if country else
             "No location stated on its website" if page else "Its website couldn't be read, so its location is unknown")
        review = _review(country) if country else ["location unknown"]
        bad, evidence = bool(review), url
        step("Checking against your Privacy Policy",
             f"{', '.join(review)} is not an approved country" if country and bad else
             "Its location can't be confirmed" if bad else f"{country} is an approved country")
        why = (f"Its new sub-processor {name} processes customer data in {country}, which is not an approved country. "
               f"{vname} doesn't list a location; Night's Watch found it on {name}'s own website." if country and bad else
               f"Its new sub-processor {name} doesn't say where it processes customer data." if bad else "")
        with _real_lock:
            subs = v.get("subprocessors") or []
            subs.append({"name": name, "country": country, "review": review, "purpose": "", "simulated": True})
            v["subprocessors"] = subs
            if v.get("subprocessor_count") is not None:
                v["subprocessor_count"] += 1
            if bad:
                v["status"] = "not_compliant"
                v["issues"] = v["issues"] + [{"rule": "R1", "title": f"New sub-processor {name}: " +
                                              (f"processes data in {country}" if country else "location unknown"),
                                              "detail": f"Found on {url}", "policy": None}]
    with _real_lock:
        _real["checks"] = _real.get("checks", 1) + 1
        _real["checked_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        _real["latest_event"] = {
            "kind": "violation", "simulated": True, "vendor": vname, "title": why,
            "headline": f"{vname} is no longer compliant with your Privacy Policy", "why": why, "policy": None,
            "evidence_url": evidence, "tick": _real["checks"], "memory_edits": [],
            "agent": {"model": "Nimble + your policy rules", "seconds": round(time.time() - t0, 1),
                      "steps": [{"text": x["text"], "detail": x["detail"]} for x in steps]}} if bad else None
        _save_real()


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
            self._json(200, app_overview())
        elif path.startswith("/api/job/"):
            j = _jobs.get(path.rsplit("/", 1)[-1])
            self._json(200 if j else 404, j or {"error": "no such job"})
        elif path == "/about" or path.startswith("/about/"):  # lane C's marketing landing page + its assets
            name = path[len("/about/"):] or "index.html"
            f = (WEB_CONSOLE.parent / name).resolve()
            ctype = {".html": "text/html; charset=utf-8", ".css": "text/css", ".js": "text/javascript",
                     ".svg": "image/svg+xml", ".png": "image/png", ".jpg": "image/jpeg", ".ico": "image/x-icon",
                     ".json": "application/json"}
            if path == "/about":
                self.send_response(301); self.send_header("Location", "/about/"); self.end_headers()
            elif f.parent == WEB_CONSOLE.parent.resolve() and f.is_file() and f.suffix in ctype:
                self._send(200, f.read_bytes(), ctype[f.suffix])
            else:
                self._send(404, b"not found", "text/plain")
        elif path == "/classic":  # lane B's original all-panels page, kept as a fallback
            self._send(200, PAGE.replace("__SUFFIX__", SUFFIX or "(stage tables)").encode(), "text/html; charset=utf-8")
        elif path == "/api/state":
            self._json(200, state())
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?")[0]
        if path not in ("/api/action", "/api/onboard", "/api/simulate", "/api/chain"):
            return self._json(404, {"error": "not found"})
        try:
            n = min(int(self.headers.get("Content-Length") or 0), 4096)
            body = json.loads(self.rfile.read(n) or b"{}")
            name = body.get("action", "")
        except Exception:
            return self._json(400, {"ok": False, "seconds": 0, "output": "bad JSON", "error": "bad JSON"})
        if path == "/api/chain":  # read one vendor's own sub-processors (real sites only), in the background
            return self._json(*_start_chain(str(body.get("slug") or "")))
        if path == "/api/onboard":
            url = str(body.get("url") or "").strip()
            if not url or len(url) > 300 or not re.match(r"^(https?://)?[A-Za-z0-9.-]+(:\d+)?(/[\w./-]*)?$", url):
                return self._json(400, {"error": "enter a website address, like https://example.com"})
            return self._json(*_launch("onboard", _onboard, url))
        if path == "/api/simulate":
            sc = str(body.get("scenario") or "")
            if sc not in SIM_SCENARIOS:
                return self._json(400, {"error": f"unknown scenario (allowed: {', '.join(SIM_SCENARIOS)})"})
            name, url = str(body.get("name") or "").strip(), str(body.get("url") or "").strip()
            if sc == "custom":
                if not re.match(r"^[\w .,&'()-]{2,60}$", name):
                    return self._json(400, {"error": "enter the company's name"})
                if len(url) > 200 or not re.match(r"^(https?://)?[A-Za-z0-9.-]+\.[A-Za-z]{2,}(:\d+)?(/[\w./-]*)?$", url):
                    return self._json(400, {"error": "enter the company's website, like https://example.com"})
                url = url if re.match(r"^https?://", url) else "https://" + url
            return self._json(*_launch("simulate", _real_simulate, sc, str(body.get("slug") or ""), name, url))
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
