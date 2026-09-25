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
            self._send(200, PAGE.replace("__SUFFIX__", SUFFIX or "(stage tables)").encode(), "text/html; charset=utf-8")
        elif path == "/api/state":
            self._json(200, state())
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path.split("?")[0] != "/api/action":
            return self._json(404, {"error": "not found"})
        try:
            n = min(int(self.headers.get("Content-Length") or 0), 4096)
            name = json.loads(self.rfile.read(n) or b"{}").get("action", "")
        except Exception:
            return self._json(400, {"ok": False, "seconds": 0, "output": "bad JSON"})
        code, body = run_action(str(name))
        self._json(code, body)

    def log_message(self, fmt, *args):
        if "/api/state" not in (args[0] if args else ""):
            super().log_message(fmt, *args)


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
 const last=t.length-1;
 $('chart').innerHTML=`<svg viewBox="0 0 ${W} ${H}" width="100%" height="150">
  <line x1="${P}" y1="${H-18}" x2="${W}" y2="${H-18}" stroke="#1f2a44"/><text x="4" y="16" fill="#8b97b8" font-size="12">${fmt(mx)}</text><text x="4" y="${H-20}" fill="#8b97b8" font-size="12">0</text>
  ${t.map((v,i)=>`<text x="${x(i)}" y="${H-3}" fill="#8b97b8" font-size="11" text-anchor="middle">t${v}</text>`).join('')}
  ${line(s.naive,'#f87171')}${line(s.nights_watch,'#34d399')}
  <text x="${x(last)-6}" y="${y(s.naive[last])-8}" fill="#f87171" font-size="13" text-anchor="end">naive ${fmt(s.naive[last])} (computed, not run)</text>
  <text x="${x(last)-6}" y="${y(s.nights_watch[last])-8}" fill="#34d399" font-size="13" text-anchor="end">Night's Watch ${fmt(s.nights_watch[last])}</text></svg>`;
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
