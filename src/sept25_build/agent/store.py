"""Where the agent reads snapshots and keeps its cards: RawTree (live) or memory (fixtures/tests)."""

from .. import rawtree
from ..contracts import TABLES, Alert, Snapshot, StateCard, TickLog


class MemoryStore:
    def __init__(self):
        self.snapshots: list[Snapshot] = []
        self.cards: dict[str, StateCard] = {}
        self.alerts: list[Alert] = []
        self.ticks: list[TickLog] = []

    def latest_snapshots(self) -> list[Snapshot]:
        latest: dict[str, Snapshot] = {}
        for s in self.snapshots:
            if s.vendor not in latest or s.fetched_at >= latest[s.vendor].fetched_at:
                latest[s.vendor] = s
        return list(latest.values())

    def latest_cards(self) -> dict[str, StateCard]:
        return dict(self.cards)

    def last_naive_total(self, vendor: str) -> int:
        rows = [t for t in self.ticks if t.vendor == vendor and t.agent == "naive"]
        return rows[-1].input_tokens if rows else 0

    def write(self, cards: list[StateCard], alerts: list[Alert], ticks: list[TickLog]) -> None:
        self.cards.update({c.vendor: c for c in cards})
        self.alerts += alerts
        self.ticks += ticks


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
        try:
            rows = rawtree.query(f"SELECT toString(vendor) AS v, toString(tick) AS t, toString(updated_at) AS u, "
                                 f"toString(card_json) AS card_json FROM {TABLES['state_cards']}")
        except RuntimeError as e:
            if _missing_table(e):  # first run: table not created yet
                return {}
            raise
        best: dict[str, dict] = {}
        for r in rows:
            k = (int(float(r["t"] or 0)), r["u"])
            if r["v"] not in best or k > best[r["v"]]["k"]:
                best[r["v"]] = {"k": k, "row": r}
        return {v: StateCard.from_row(b["row"]) for v, b in best.items()}

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

    def write(self, cards: list[StateCard], alerts: list[Alert], ticks: list[TickLog]) -> None:
        # Cards last: if an earlier insert fails, the next tick re-processes the same snapshots
        # instead of believing it already raised their alerts.
        rawtree.insert("alerts", [a.to_row() for a in alerts])
        rawtree.insert("ticks", [t.to_row() for t in ticks])
        rawtree.insert("state_cards", [c.to_row() for c in cards])
