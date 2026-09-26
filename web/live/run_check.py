"""Run lane B's any-website check (agent/console.py: _onboard -> _discover) headless, for GitHub Actions.

    uv run --env-file .env python web/live/run_check.py https://example.com            # prints the result
    python run_check.py URL --id ID --report https://.../check/ID/report                  # in the Action

Needs NIMBLE_API_KEY only. With --report it POSTs progress every 2 s and the final result to the Worker,
authorised by REPORT_SECRET; the Worker stores them in D1. The result is lane B's /api/overview for a real
site (console._real_overview()), so the demo page renders it with lane B's own real-site view.
"""

import argparse
import json
import os
import sys
import threading
import urllib.request

from sept25_build.agent import console


def post(url: str, body: dict) -> None:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers={
        "Content-Type": "application/json", "Authorization": f"Bearer {os.environ.get('REPORT_SECRET', '')}",
        "User-Agent": "nights-watch-live"})
    try:
        urllib.request.urlopen(req, timeout=20).read()
    except Exception as e:  # noqa: BLE001 - a lost progress update is fine; the final one is retried below
        print(f"report failed: {type(e).__name__}: {e}", file=sys.stderr)
        raise


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--report", default="")
    a = ap.parse_args()

    job = console._Job("onboard")
    done = threading.Event()

    def work() -> None:
        try:
            console._onboard(job, a.url)
            job.finish()
        except Exception as e:  # noqa: BLE001
            # lane B raises RuntimeError with a sentence meant for people; anything else is a bug, so name it
            job.finish(str(e)[:300] if isinstance(e, RuntimeError) else f"{type(e).__name__}: {str(e)[:300]}")
        finally:
            done.set()

    threading.Thread(target=work, daemon=True).start()
    last = ""
    while not done.wait(2):
        now = json.dumps(job.d["steps"])
        if a.report and now != last:
            last = now
            try:
                post(a.report, {"status": "running", "steps": job.d["steps"]})
            except Exception:  # noqa: BLE001
                pass
    ok = job.d["status"] == "done"
    body = {"status": "done" if ok else "failed", "steps": job.d["steps"], "error": job.d["error"],
            "result": console._real_overview() if ok else None}
    if not a.report:
        print(json.dumps(body, indent=1))
        return
    for attempt in range(3):
        try:
            post(a.report, body)
            break
        except Exception:  # noqa: BLE001
            if attempt == 2:
                sys.exit("could not report the result")
    print(f"{body['status']}: {len((body['result'] or {}).get('vendors') or [])} sub-processors")


if __name__ == "__main__":
    main()
