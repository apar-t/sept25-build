"""Minimal RawTree client (Tinybird's event database). SHARED: owned by C, used by all lanes.

API: https://rawtree.com/openapi.json
  insert: POST /v1/tables/<table>  (JSON array; creates the table on first insert)
  query:  POST /v1/query {"sql": ...}  (read-only SQL, ClickHouse dialect)
"""

import os

import requests
from dotenv import load_dotenv

from .contracts import TABLES

load_dotenv()

API = "https://api.rawtree.com/v1"


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {os.environ['RAWTREE_API_KEY']}",
        "x-rawtree-database": os.environ.get("RAWTREE_DATABASE", "default"),
        "content-type": "application/json",
    }


def insert(table: str, rows: list[dict]) -> None:
    """Append rows. `table` may be a TABLES key ("alerts") or a full name ("nw_alerts")."""
    if not rows:
        return
    name = TABLES.get(table, table)
    if not name.startswith("nw_"):
        raise ValueError(f"table {name!r} must be prefixed nw_ (the database is shared)")
    r = requests.post(f"{API}/tables/{name}", headers=_headers(), json=rows, timeout=30)
    if not r.ok:
        raise RuntimeError(f"RawTree insert into {name} failed {r.status_code}: {r.text[:300]}")


def query(sql: str) -> list[dict]:
    """Run read-only SQL and return rows as dicts."""
    r = requests.post(f"{API}/query", headers=_headers(), json={"sql": sql}, timeout=30)
    if not r.ok:
        raise RuntimeError(f"RawTree query failed {r.status_code}: {r.text[:300]}")
    return r.json()["data"]
