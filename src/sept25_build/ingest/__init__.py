"""Lane A: ingest (Nimble). Owner: rohitenterprise.

Reads each vendor's demo mirror on site/ (see site/README.md) through Nimble and appends
contracts.Snapshot rows to nw_snapshots.
Interface the other lanes rely on:
    fetch_vendor(vendor: str) -> Snapshot | None   # None = the read failed, so nothing gets saved
    run_once(vendors: list[str] | None = None) -> list[Snapshot]   # read, insert into nw_snapshots, return them

    uv run python -m sept25_build.ingest            # every vendor
    uv run python -m sept25_build.ingest tinybird   # only the vendor you just injected into

The site URL is SITE_URL if set, else site/live/tunnel_url.txt (written by `bash site/tunnel.sh`).
"""

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from functools import cache
from pathlib import Path

import requests
from dotenv import load_dotenv
from nimble_python import Nimble

from .. import rawtree
from ..contracts import VENDORS, Snapshot

load_dotenv()

TUNNEL_URL_FILE = Path(__file__).resolve().parents[3] / "site" / "live" / "tunnel_url.txt"


@cache
def _nimble() -> Nimble:
    return Nimble(api_key=os.environ["NIMBLE_API_KEY"], timeout=20, max_retries=0)


def _site_url() -> str:
    url = os.environ.get("SITE_URL") or (TUNNEL_URL_FILE.read_text() if TUNNEL_URL_FILE.exists() else "")
    if not url.strip():
        raise RuntimeError("no demo site URL: run `bash site/tunnel.sh` first (or set SITE_URL)")
    return url.strip().rstrip("/")


def _read(url: str) -> tuple[str, str]:
    """(file exactly as served, how it was read). Nimble first; direct only if Nimble errors."""
    try:
        # "html" returns the raw file; "markdown" re-flows the JSON and merges the DPA's paragraphs
        r = _nimble().extract.run(url=url, formats=["html"], render=False)
        status, body, via = r.status_code or 200, r.data.html or "", "Nimble"
    except Exception as e:  # e.g. a just-started tunnel stays unreachable for Nimble for about a minute
        resp = requests.get(url, timeout=10)
        status, body, via = resp.status_code, resp.text, f"direct, Nimble {type(e).__name__}"
    if status >= 400:  # Nimble reports a 404/502 as success, with the error page as the body
        raise RuntimeError(f"HTTP {status:.0f} for {url}")
    if not body.strip():
        raise RuntimeError(f"empty page: {url}")
    return body, via


def fetch_vendor(vendor: str) -> Snapshot | None:
    label, page = f"{VENDORS.get(vendor, vendor)} (demo mirror)", f"{_site_url()}/vendors/{vendor}"
    t = time.monotonic()
    try:
        with ThreadPoolExecutor(2) as pool:
            (subs, via1), (dpa, via2) = pool.map(_read, [f"{page}/subprocessors.json", f"{page}/dpa.md"])
        snap = Snapshot(vendor=f"{vendor}-mirror", display_name=label,
                        is_demo_mirror=True, source_urls=[f"{page}/subprocessors", f"{page}/dpa"],
                        subprocessors=json.loads(subs), policy_text=dpa)
    except Exception as e:  # a failed read saves nothing; the agent keeps the vendor's last good snapshot
        print(f"  {label:32} FAILED  {type(e).__name__}: {str(e)[:120]}")
        return None
    via = via1 if via1 == via2 else f"{via1} / {via2}"
    print(f"  {label:32} {len(snap.subprocessors)} companies  {time.monotonic() - t:.1f}s via {via}")
    return snap


def run_once(vendors: list[str] | None = None) -> list[Snapshot]:
    vendors = vendors or list(VENDORS)
    unknown = [v for v in vendors if v not in VENDORS]
    if unknown:
        raise ValueError(f"unknown vendor {', '.join(unknown)}; choose from {', '.join(VENDORS)}")
    _site_url()  # fail once, clearly, before starting any reads
    with ThreadPoolExecutor(len(vendors)) as pool:
        snaps = [s for s in pool.map(fetch_vendor, vendors) if s]
    rawtree.insert("snapshots", [s.to_row() for s in snaps])
    print(f"saved {len(snaps)} of {len(vendors)} vendors to nw_snapshots")
    return snaps
