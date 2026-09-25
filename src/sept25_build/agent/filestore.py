"""Durable local store: one append-only JSONL file per table under `root`. Crash-safe for a tick.

- Every append is flushed and fsync'd. A torn last line (process killed mid-write) is skipped on
  read, and the next append starts on a fresh line, so the torn bytes never corrupt a good row.
- write() order is ops, alerts, ticks, then cards LAST: the cards are the tick's commit point.
  All cards of one write() carry a batch id and size (`_batch`, `_n`); a batch only counts once
  every one of its lines is on disk, so a crash halfway through the cards leaves the whole tick
  uncommitted and it is replayed with the same tick number.
- Alerts, ticks and memory ops are skipped if their idempotency key (alert_id, tick_id, op_id) is
  already on disk, so a replayed tick adds nothing twice.
"""

import json
import os
import uuid
from collections import Counter
from datetime import datetime
from pathlib import Path

from ..contracts import Alert, MemoryOp, Snapshot, StateCard, TickLog

FILES = {"snapshots": "snapshots.jsonl", "state_cards": "state_cards.jsonl", "alerts": "alerts.jsonl",
         "ticks": "ticks.jsonl", "memory_ops": "memory_ops.jsonl"}
KEYS = {"memory_ops": "op_id", "alerts": "alert_id", "ticks": "tick_id"}  # also the write order


class FileStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, table: str) -> Path:
        return self.root / FILES[table]

    def rows(self, table: str) -> list[dict]:
        """Every parseable row, in file order. Torn or garbage lines are skipped."""
        p, out = self.path(table), []
        if not p.exists():
            return out
        with p.open("rb") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:  # torn line from a crash mid-write (incl. broken UTF-8)
                    continue
                if isinstance(r, dict):
                    out.append(r)
        return out

    # ---- writing -------------------------------------------------------------------------------
    @staticmethod
    def _lines(rows: list[dict]) -> list[bytes]:
        return [(json.dumps(r, ensure_ascii=False, default=str) + "\n").encode() for r in rows]

    def _append_raw(self, table: str, data: bytes) -> None:
        p = self.path(table)
        if p.exists() and p.stat().st_size:
            with p.open("rb") as f:
                f.seek(-1, os.SEEK_END)
                if f.read(1) != b"\n":
                    data = b"\n" + data  # previous writer died mid-line: start a fresh line
        with p.open("ab") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())

    def _append(self, table: str, rows: list[dict]) -> None:
        if rows:
            self._append_raw(table, b"".join(self._lines(rows)))

    def add_snapshots(self, snaps: list[Snapshot]) -> None:
        self._append("snapshots", [s.to_row() for s in snaps])

    def write(self, cards: list[StateCard], alerts: list[Alert], ticks: list[TickLog], ops=()) -> None:
        for table, items in (("memory_ops", ops), ("alerts", alerts), ("ticks", ticks)):
            key = KEYS[table]
            seen = {r.get(key) for r in self.rows(table)}
            fresh = []
            for m in items or ():
                if getattr(m, key) not in seen:
                    seen.add(getattr(m, key))
                    fresh.append(m.to_row())
            self._append(table, fresh)
        batch = uuid.uuid4().hex
        self._append("state_cards", [dict(c.to_row(), _batch=batch, _n=len(cards)) for c in cards])

    # ---- reading -------------------------------------------------------------------------------
    def latest_snapshots(self) -> list[Snapshot]:
        latest: dict[str, tuple[datetime, dict]] = {}
        for r in self.rows("snapshots"):
            try:
                at = datetime.fromisoformat(r["fetched_at"])
            except (KeyError, TypeError, ValueError):
                continue
            if r.get("vendor") not in latest or at >= latest[r["vendor"]][0]:
                latest[r["vendor"]] = (at, r)
        out = []
        for _, r in latest.values():
            try:
                out.append(Snapshot.from_row(r))
            except Exception as e:  # one malformed row must not stop every other vendor
                print(f"filestore: skipping malformed snapshot {r.get('snapshot_id')!r}: {e}")
        return out

    def snapshots_for(self, vendor: str) -> list[Snapshot]:
        out = []
        for r in self.rows("snapshots"):
            if r.get("vendor") == vendor:
                try:
                    out.append(Snapshot.from_row(r))
                except Exception:
                    continue
        return sorted(out, key=lambda s: s.fetched_at)

    def latest_cards(self) -> dict[str, StateCard]:
        rows = self.rows("state_cards")
        have = Counter(r["_batch"] for r in rows if "_batch" in r)
        best: dict[str, tuple] = {}
        for r in rows:
            if "_batch" in r and have[r["_batch"]] < r.get("_n", 0):
                continue  # batch never fully written: that tick was not committed
            try:
                c = StateCard.from_row(r)
            except Exception:
                continue
            k = (c.tick, c.updated_at)
            if c.vendor not in best or k >= best[c.vendor][0]:  # ties: later line wins
                best[c.vendor] = (k, c)
        return {v: c for v, (_, c) in best.items()}

    def last_naive_total(self, vendor: str, before_tick: int | None = None) -> int:
        best = None
        for r in self.rows("ticks"):
            if r.get("vendor") == vendor and r.get("agent") == "naive" and \
                    (before_tick is None or int(r.get("tick", 0)) < before_tick):
                if best is None or int(r.get("tick", 0)) >= int(best.get("tick", 0)):
                    best = r
        return int(best.get("input_tokens", 0)) if best else 0

    def _models(self, table: str, model):
        out = []
        for r in self.rows(table):
            try:
                out.append(model.model_validate(r))
            except Exception:
                continue
        return out

    def all_alerts(self) -> list[Alert]:
        return self._models("alerts", Alert)

    def all_ticks(self) -> list[TickLog]:
        return self._models("ticks", TickLog)

    def all_ops(self) -> list[MemoryOp]:
        return self._models("memory_ops", MemoryOp)
