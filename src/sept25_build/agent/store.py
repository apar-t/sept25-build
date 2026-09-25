"""Where the agent reads snapshots and keeps its memory: RawTree (live), memory (tests), files (filestore.py).

Every store has the same contract:
  latest_snapshots(), latest_cards(), last_naive_total(vendor), snapshots_for(vendor)
  write(cards, alerts, ticks, ops): ops, alerts, ticks first, cards LAST (the commit point);
      rows whose idempotency key (alert_id / tick_id / op_id) was already written are skipped
"""

from .. import rawtree
from ..contracts import TABLES, Alert, MemoryOp, Snapshot, StateCard, TickLog


class MemoryStore:
    def __init__(self):
        self.snapshots: list[Snapshot] = []
        self.cards: dict[str, StateCard] = {}
        self.alerts: list[Alert] = []
        self.ticks: list[TickLog] = []
        self.ops: list[MemoryOp] = []

    def add_snapshots(self, snaps: list[Snapshot]) -> None:
        self.snapshots += snaps

    def latest_snapshots(self) -> list[Snapshot]:
        latest: dict[str, Snapshot] = {}
        for s in self.snapshots:
            if s.vendor not in latest or s.fetched_at >= latest[s.vendor].fetched_at:
                latest[s.vendor] = s
        return list(latest.values())

    def snapshots_for(self, vendor: str) -> list[Snapshot]:
        return sorted((s for s in self.snapshots if s.vendor == vendor), key=lambda s: s.fetched_at)

    def latest_cards(self) -> dict[str, StateCard]:
        return dict(self.cards)

    def last_naive_total(self, vendor: str) -> int:
        rows = [t for t in self.ticks if t.vendor == vendor and t.agent == "naive"]
        return rows[-1].input_tokens if rows else 0

    def write(self, cards: list[StateCard], alerts: list[Alert], ticks: list[TickLog], ops=()) -> None:
        seen_a, seen_t, seen_o = {a.alert_id for a in self.alerts}, {t.tick_id for t in self.ticks}, {o.op_id for o in self.ops}
        self.ops += [o for o in ops if o.op_id not in seen_o]
        self.alerts += [a for a in alerts if a.alert_id not in seen_a]
        self.ticks += [t for t in ticks if t.tick_id not in seen_t]
        self.cards.update({c.vendor: c for c in cards})


def _missing_table(e: Exception) -> bool:
    msg = str(e).lower()
    return "exist" in msg or "unknown table" in msg or "not found" in msg


class RawTreeStore:
    """RawTree columns are Dynamic, so we never ORDER BY them in SQL: pick the latest rows in Python."""

    def latest_snapshots(self) -> list[Snapshot]:
        t = TABLES["snapshots"]
        try:
            ids = rawtree.query(f"SELECT toString(snapshot_id) AS id, toString(vendor) AS v, "
                                f"toString(fetched_at) AS f FROM {t}")
        except RuntimeError as e:
            if _missing_table(e):
                return []
            raise
        latest: dict[str, dict] = {}
        for r in ids:
            if r["v"] not in latest or r["f"] > latest[r["v"]]["f"]:  # ISO-8601 UTC sorts as text
                latest[r["v"]] = r
        if not latest:
            return []
        wanted = ", ".join("'" + r["id"].replace("'", "''") + "'" for r in latest.values())
        rows = rawtree.query(f"SELECT * FROM {t} WHERE toString(snapshot_id) IN ({wanted})")
        out, seen = [], set()
        for r in rows:
            try:
                snap = Snapshot.from_row(r)
            except Exception as e:  # one malformed row must not stop every other vendor
                print(f"skipping malformed snapshot row {r.get('snapshot_id')!r}: {e}")
                continue
            if snap.vendor not in seen:
                seen.add(snap.vendor)
                out.append(snap)
        return out

    def latest_cards(self) -> dict[str, StateCard]:
        """Latest committed card per vendor. A corrupt row falls back to that vendor's previous good one."""
        try:
            rows = rawtree.query(f"SELECT toString(vendor) AS v, toString(tick) AS t, toString(updated_at) AS u, "
                                 f"toString(card_json) AS card_json FROM {TABLES['state_cards']}")
        except RuntimeError as e:
            if _missing_table(e):  # first run: table not created yet
                return {}
            raise
        by_vendor: dict[str, list] = {}
        for r in rows:
            by_vendor.setdefault(r["v"], []).append(r)
        out = {}
        for v, rs in by_vendor.items():
            for r in sorted(rs, key=lambda r: (int(float(r["t"] or 0)), r["u"]), reverse=True):
                try:
                    out[v] = StateCard.from_row(r)
                    break
                except Exception as e:
                    print(f"recovery: skipping corrupt card row for {v} at tick {r['t']}: {type(e).__name__}")
        return out

    def snapshots_for(self, vendor: str) -> list[Snapshot]:
        v = vendor.replace("'", "''")
        rows = rawtree.query(f"SELECT * FROM {TABLES['snapshots']} WHERE toString(vendor) = '{v}'")
        snaps = []
        for r in rows:
            try:
                snaps.append(Snapshot.from_row(r))
            except Exception:
                continue
        return sorted(snaps, key=lambda s: s.fetched_at)

    def _existing(self, table: str, key: str, ids: list[str]) -> set[str]:
        if not ids:
            return set()
        wanted = ", ".join("'" + i.replace("'", "''") + "'" for i in ids)
        try:
            rows = rawtree.query(f"SELECT toString({key}) AS id FROM {TABLES[table]} WHERE toString({key}) IN ({wanted})")
        except RuntimeError as e:
            if _missing_table(e):
                return set()
            raise
        return {r["id"] for r in rows}

    def last_naive_total(self, vendor: str) -> int:
        try:
            rows = rawtree.query(f"SELECT toString(tick) AS t, toString(input_tokens) AS n FROM {TABLES['ticks']} "
                                 f"WHERE toString(vendor) = '{vendor.replace(chr(39), chr(39) * 2)}' "
                                 f"AND toString(agent) = 'naive' AND toString(run_id) = 'live'")
        except RuntimeError:
            return 0
        if not rows:
            return 0
        return int(float(max(rows, key=lambda r: int(float(r["t"])))["n"]))

    def write(self, cards: list[StateCard], alerts: list[Alert], ticks: list[TickLog], ops=()) -> None:
        # Cards last: they are the commit point. If anything before them fails, the next run
        # replays the tick with the same ids, and rows that already made it are skipped here.
        ops, alerts, ticks = list(ops), list(alerts), list(ticks)
        have = self._existing("memory_ops", "op_id", [o.op_id for o in ops])
        rawtree.insert("memory_ops", [o.to_row() for o in ops if o.op_id not in have])
        have = self._existing("alerts", "alert_id", [a.alert_id for a in alerts])
        rawtree.insert("alerts", [a.to_row() for a in alerts if a.alert_id not in have])
        have = self._existing("ticks", "tick_id", [t.tick_id for t in ticks])
        rawtree.insert("ticks", [t.to_row() for t in ticks if t.tick_id not in have])
        rawtree.insert("state_cards", [c.to_row() for c in cards])
