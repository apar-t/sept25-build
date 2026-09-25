"""Night's Watch Inc. Trust Center + vendor demo mirrors. Stdlib only.

Every request is rendered from JSON in site/live/, so an injection (site/inject.py) shows up on
the very next fetch. Every response is sent with Cache-Control: no-store.

    uv run python site/serve.py [--port 8765] [--host 127.0.0.1]
"""

import argparse
import html
import json
import re
import shutil
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SITE = Path(__file__).resolve().parent
SEED, LIVE = SITE / "seed", SITE / "live"
SLUGS = ["aws", "nimble", "tinybird", "liquid", "bfl"]

CSS = """
body{margin:0;font:16px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;color:#1d232b;background:#f6f7f9}
header{background:#141a22;color:#fff}header .wrap{padding:14px 20px}header a{color:#fff;text-decoration:none;font-weight:600}
header nav a{font-weight:400;margin-left:18px;color:#c9d3df}
.wrap{max-width:860px;margin:0 auto;padding:24px 20px}
main.wrap{background:#fff;border:1px solid #e3e6ea;border-radius:8px;margin-top:24px;margin-bottom:24px}
h1{font-size:1.7em;margin:.2em 0 .5em}h2{font-size:1.2em;margin:1.4em 0 .4em}
a{color:#1f5fbf}
table{border-collapse:collapse;width:100%;margin:12px 0}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid #e3e6ea;vertical-align:top}
th{background:#f0f2f5;font-weight:600}
.banner{background:#fff4d6;border:2px solid #e0a800;color:#5a4300;padding:12px 16px;border-radius:6px;font-weight:600;margin-bottom:18px}
.muted{color:#5f6b7a;font-size:.92em}
footer{color:#5f6b7a;font-size:.85em;text-align:center;padding:0 20px 30px}
ul.links li{margin:6px 0}
"""


def ensure_live() -> None:
    """Create site/live/ from site/seed/, copying any file that is missing (never overwrites)."""
    for src in SEED.rglob("*.json"):
        dst = LIVE / src.relative_to(SEED)
        if not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)


def load(rel: str) -> dict:
    return json.loads((LIVE / rel).read_text())


def esc(s: str) -> str:
    return html.escape(str(s), quote=False)


def inline(s: str) -> str:
    """Escape text, then turn [text](/path) into a link (company pages only use local links)."""
    return re.sub(r"\[([^\]]+)\]\((/[^)\s]*)\)", r'<a href="\2">\1</a>', esc(s))


def md_to_html(md: str) -> str:
    """Tiny markdown: '# ' and '## ' headings, paragraphs separated by blank lines."""
    out = []
    for block in re.split(r"\n\s*\n", md.strip()):
        para = []
        for line in block.splitlines():
            m = re.match(r"^(#{1,2})\s+(.*)$", line.strip())
            if m:
                if para:
                    out.append(f"<p>{esc(' '.join(para))}</p>")
                    para = []
                n = len(m.group(1))
                out.append(f"<h{n}>{esc(m.group(2))}</h{n}>")
            elif line.strip():
                para.append(line.strip())
        if para:
            out.append(f"<p>{esc(' '.join(para))}</p>")
    return "\n".join(out)


def page(title: str, body: str, banner: str = "") -> str:
    company = load("company.json")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title><style>{CSS}</style></head>
<body>
<header><div class="wrap"><a href="/">{esc(company["name"])} Trust Center</a>
<nav style="display:inline"><a href="/privacy">Privacy</a><a href="/terms">Terms</a><a href="/subprocessors">Sub-processors</a></nav></div></header>
<main class="wrap">
{f'<div class="banner" role="note">{esc(banner)}</div>' if banner else ''}
{body}
</main>
<footer>{esc(company["disclaimer"])}</footer>
</body></html>
"""


def poss(name: str) -> str:
    return name + ("'" if name.endswith("s") else "'s")


def mirror_banner(name: str) -> str:
    return (f"Demo mirror for the Night's Watch hackathon project. Illustrative content — "
            f"not {poss(name)} actual terms; not affiliated with {name}.")


def sections(items: list[dict]) -> str:
    return "\n".join(f"<h2>{i}. {esc(s['heading'])}</h2>\n<p>{inline(s['body'])}</p>"
                     for i, s in enumerate(items, 1))


# ---- routes -----------------------------------------------------------------------------------

def r_home() -> str:
    c = load("company.json")
    vendors = "\n".join(
        f'<li><strong>{esc(v["name"])}</strong> — <a href="/vendors/{v["slug"]}/subprocessors">sub-processors</a>'
        f' · <a href="/vendors/{v["slug"]}/dpa">data-processing terms</a> <span class="muted">(demo mirror)</span></li>'
        for v in c["subprocessors"])
    body = f"""<h1>{esc(c["name"])} Trust Center</h1>
<p>{esc(c["intro"])}</p>
<h2>Our policies</h2>
<ul class="links">
<li><a href="/privacy">Privacy policy</a></li>
<li><a href="/terms">Terms of use</a></li>
<li><a href="/subprocessors">Sub-processors</a> — the vendors that process customer data for us</li>
</ul>
<h2>Our vendors' terms (demo mirrors)</h2>
<p class="muted">Illustrative copies hosted for the Night's Watch hackathon demo. Not the vendors' actual terms; not affiliated with them.</p>
<ul class="links">
{vendors}
</ul>"""
    return page(f"{c['name']} Trust Center", body)


def r_privacy() -> str:
    c = load("company.json")
    body = (f"<h1>{esc(c['name'])} Privacy Policy</h1>\n<p class=\"muted\">Last updated: {esc(c['updated'])}</p>\n"
            + sections(c["privacy"]))
    return page(f"Privacy Policy — {c['name']}", body)


def r_terms() -> str:
    c = load("company.json")
    body = (f"<h1>{esc(c['name'])} Terms of Use</h1>\n<p class=\"muted\">Last updated: {esc(c['updated'])}</p>\n"
            + sections(c["terms"]))
    return page(f"Terms of Use — {c['name']}", body)


def r_company_subs() -> str:
    c = load("company.json")
    rows = "\n".join(
        f'<tr><td>{esc(v["name"])}</td><td>{esc(v["purpose"])}</td><td>{esc(v["location"])}</td>'
        f'<td><a href="/vendors/{v["slug"]}/">Details</a></td></tr>' for v in c["subprocessors"])
    body = f"""<h1>{esc(c["name"])} Sub-processors</h1>
<p>Each vendor below processes customer data for {esc(c["name"])} under written data-processing terms.</p>
<table><thead><tr><th>Name</th><th>Purpose</th><th>Location</th><th>Details</th></tr></thead>
<tbody>
{rows}
</tbody></table>"""
    return page(f"Sub-processors — {c['name']}", body)


def r_vendor_index(v: dict) -> str:
    n, s = v["display_name"], v["slug"]
    body = f"""<h1>{esc(n)} (demo mirror)</h1>
<p>{esc(n)} is a sub-processor of Night's Watch Inc. This mirror holds illustrative copies of its sub-processor list and data-processing terms.</p>
<ul class="links">
<li><a href="/vendors/{s}/subprocessors">{esc(n)} sub-processors</a> (<a href="/vendors/{s}/subprocessors.json">JSON</a>)</li>
<li><a href="/vendors/{s}/dpa">{esc(n)} data-processing terms</a> (<a href="/vendors/{s}/dpa.md">markdown</a>)</li>
</ul>"""
    return page(f"{n} (demo mirror)", body, mirror_banner(n))


def r_vendor_subs(v: dict) -> str:
    n = v["display_name"]
    rows = "\n".join(f"<tr><td>{esc(sp['name'])}</td><td>{esc(sp['purpose'])}</td><td>{esc(sp['country'])}</td></tr>"
                     for sp in v["subprocessors"])
    body = f"""<h1>{esc(n)} sub-processors (demo mirror)</h1>
<p>{esc(n)} uses the following sub-processors to provide its services.</p>
<table><thead><tr><th>Name</th><th>Purpose</th><th>Location</th></tr></thead>
<tbody>
{rows}
</tbody></table>"""
    return page(f"{n} sub-processors (demo mirror)", body, mirror_banner(n))


def r_vendor_dpa(v: dict) -> str:
    n = v["display_name"]
    return page(f"{n} data-processing terms (demo mirror)",
                f"<article>\n{md_to_html(v['dpa_markdown'])}\n</article>", mirror_banner(n))


def route(path: str) -> tuple[int, str, str]:
    """-> (status, content_type, body)"""
    path = path.split("?", 1)[0].split("#", 1)[0]
    if path != "/":
        path = path.rstrip("/")
    simple = {"/": r_home, "/privacy": r_privacy, "/terms": r_terms, "/subprocessors": r_company_subs}
    if path in simple:
        return 200, "text/html; charset=utf-8", simple[path]()
    m = re.fullmatch(r"/vendors/([a-z0-9-]+)(?:/(subprocessors|subprocessors\.json|dpa|dpa\.md))?", path)
    if m and (LIVE / "vendors" / f"{m.group(1)}.json").exists():
        v = load(f"vendors/{m.group(1)}.json")
        sub = m.group(2)
        if sub is None:
            return 200, "text/html; charset=utf-8", r_vendor_index(v)
        if sub == "subprocessors":
            return 200, "text/html; charset=utf-8", r_vendor_subs(v)
        if sub == "subprocessors.json":
            data = [{"name": s["name"], "purpose": s["purpose"], "country": s["country"]} for s in v["subprocessors"]]
            return 200, "application/json; charset=utf-8", json.dumps(data, indent=2, ensure_ascii=False) + "\n"
        if sub == "dpa":
            return 200, "text/html; charset=utf-8", r_vendor_dpa(v)
        if sub == "dpa.md":
            return 200, "text/markdown; charset=utf-8", v["dpa_markdown"]
    return 404, "text/html; charset=utf-8", page("Not found", "<h1>404 — Not found</h1><p><a href=\"/\">Back to the Trust Center</a></p>")


class Handler(BaseHTTPRequestHandler):
    server_version = "NightsWatchTrust/1.0"

    def _send(self, head_only: bool) -> None:
        try:
            status, ctype, body = route(self.path)
        except Exception as e:  # a half-edited JSON file shouldn't kill the demo
            status, ctype, body = 500, "text/plain; charset=utf-8", f"server error: {e}\n"
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.end_headers()
        if not head_only:
            self.wfile.write(data)

    def do_GET(self) -> None:
        self._send(False)

    def do_HEAD(self) -> None:
        self._send(True)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    ensure_live()
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Night's Watch Trust Center on http://{args.host}:{args.port}/  (data: {LIVE})", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
