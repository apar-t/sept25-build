"""Minimal Black Forest Labs (FLUX) client: submit, poll, download.

Docs: https://docs.bfl.ai (API reference at https://api.bfl.ai/openapi.json)
"""

import os
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

API = "https://api.bfl.ai/v1"
FAILED = {"Error", "Failed", "Request Moderated", "Content Moderated"}


def _headers() -> dict:
    return {"accept": "application/json", "x-key": os.environ["BFL_API_KEY"]}


def credits() -> float:
    """Remaining credit balance (1 credit = $0.01). Free call; use it to check the key."""
    r = requests.get(f"{API}/credits", headers=_headers(), timeout=15)
    r.raise_for_status()
    return r.json()["credits"]


def generate(prompt: str, out: str | Path, model: str = "flux-2-pro-preview",
             timeout: float = 120, **params) -> Path:
    """Text-to-image, or an edit when params include input_image (URL or base64).

    Blocks until the result is ready, then saves it to `out`. Result URLs expire
    after 10 minutes, so it downloads straight away.
    """
    r = requests.post(f"{API}/{model}", headers=_headers(),
                      json={"prompt": prompt, **params}, timeout=30)
    r.raise_for_status()
    job = r.json()

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(0.5)
        # always poll the returned polling_url; it may point at a regional host
        res = requests.get(job["polling_url"], headers=_headers(),
                           params={"id": job["id"]}, timeout=15).json()
        if res["status"] == "Ready":
            out = Path(out)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(requests.get(res["result"]["sample"], timeout=60).content)
            return out
        if res["status"] in FAILED:
            raise RuntimeError(f"BFL job {job['id']} {res['status']}: {res}")
    raise TimeoutError(f"BFL job {job['id']} not ready after {timeout}s")
