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


class RawTreeStore:
    def latest_snapshots(self) -> list[Snapshot]:
        rows = rawtree.query(f"SELECT * FROM {TABLES['snapshots']} ORDER BY fetched_at DESC LIMIT 1 BY vendor")
        return [Snapshot.from_row(r) for r in rows]

    def latest_cards(self) -> dict[str, StateCard]:
        try:
            rows = rawtree.query(f"SELECT card_json FROM {TABLES['state_cards']} ORDER BY updated_at DESC, tick DESC LIMIT 1 BY vendor")
        except RuntimeError as e:
            if "exist" in str(e).lower():  # first run: table not created yet
                return {}
            raise
        cards = [StateCard.from_row(r) for r in rows]
        return {c.vendor: c for c in cards}

    def last_naive_total(self, vendor: str) -> int:
        try:
            rows = rawtree.query(f"SELECT input_tokens FROM {TABLES['ticks']} WHERE vendor = '{vendor.replace("'", "''")}' "
                                 "AND agent = 'naive' ORDER BY tick DESC LIMIT 1")
        except RuntimeError:
            return 0
        return int(rows[0]["input_tokens"]) if rows else 0

    def write(self, cards: list[StateCard], alerts: list[Alert], ticks: list[TickLog]) -> None:
        rawtree.insert("state_cards", [c.to_row() for c in cards])
        rawtree.insert("alerts", [a.to_row() for a in alerts])
        rawtree.insert("ticks", [t.to_row() for t in ticks])
