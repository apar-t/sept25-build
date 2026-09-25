"""Shared data contracts for Night's Watch. Every lane imports from here.

SHARED FILE: don't change a field's name or meaning without telling the team first.
Adding an optional field with a default is fine; say so in the commit message.

Flow of one tick:
  ingest (A)  -> Snapshot   -> nw_snapshots
  agent  (B)  -> StateCard  -> nw_state_cards   (latest row per vendor = current state)
              -> Alert      -> nw_alerts
              -> TickLog    -> nw_ticks         (token chart: nights_watch vs naive)
  web    (C)  reads the four tables
"""

import hashlib
import json
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field

COMPANY = "Night's Watch Inc."

# RawTree tables. The database is shared with other teams, so everything is prefixed nw_.
TABLES = {
    "snapshots": "nw_snapshots",
    "state_cards": "nw_state_cards",
    "alerts": "nw_alerts",
    "ticks": "nw_ticks",
}

# The company's data policy. The agent checks every vendor against these rules.
# APPROVED_COUNTRIES is the company's own approved list, not a legal adequacy list.
# Edit it here if real sponsor sub-processors make the baseline graph red.
RULES = {
    "R1": "Customer data may only be processed in approved countries",
    "R2": "No vendor or sub-processor may use customer data to train AI models",
    "R3": "Customer data retention must be 90 days or less",
}
APPROVED_COUNTRIES = [
    # EU / EEA
    "Austria", "Belgium", "Bulgaria", "Croatia", "Cyprus", "Czech Republic", "Denmark",
    "Estonia", "Finland", "France", "Germany", "Greece", "Hungary", "Ireland", "Italy",
    "Latvia", "Lithuania", "Luxembourg", "Malta", "Netherlands", "Poland", "Portugal",
    "Romania", "Slovakia", "Slovenia", "Spain", "Sweden", "Iceland", "Liechtenstein", "Norway",
    # other approved
    "United Kingdom", "Switzerland", "United States", "Canada", "Japan",
]
MAX_RETENTION_DAYS = 90

RuleId = Literal["R1", "R2", "R3"]


def now() -> datetime:
    return datetime.now(timezone.utc)


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _truthy(v) -> bool:
    # RawTree columns are Dynamic, so booleans can come back as bool, int or string
    return v in (True, 1, "1", "true", "True")


class Subprocessor(BaseModel):
    name: str
    purpose: str = ""
    country: str = ""  # as written on the vendor's page; the agent normalizes it


class Snapshot(BaseModel):
    """One observation of one vendor. Written by ingest (A)."""
    vendor: str                      # slug: aws | nimble | tinybird | liquid | bfl | <x>-mirror
    display_name: str                # "Tinybird (demo mirror)" for the injectable copy
    fetched_at: datetime = Field(default_factory=now)
    source_urls: list[str] = []
    is_demo_mirror: bool = False
    subprocessors: list[Subprocessor] = []
    policy_text: str = ""            # privacy/DPA text as markdown, as fetched
    snapshot_id: str = ""            # filled by model_post_init if empty

    def model_post_init(self, _ctx) -> None:
        if not self.snapshot_id:
            self.snapshot_id = f"{self.vendor}:{self.fetched_at.isoformat()}"

    @property
    def policy_hash(self) -> str:
        return sha(self.policy_text)

    def to_row(self) -> dict:
        return {
            "snapshot_id": self.snapshot_id, "vendor": self.vendor,
            "display_name": self.display_name, "fetched_at": self.fetched_at.isoformat(),
            "is_demo_mirror": self.is_demo_mirror, "source_urls": json.dumps(self.source_urls),
            "subprocessors_json": json.dumps([s.model_dump() for s in self.subprocessors]),
            "subprocessor_count": len(self.subprocessors),
            "policy_text": self.policy_text, "policy_hash": self.policy_hash,
        }

    @classmethod
    def from_row(cls, row: dict) -> "Snapshot":
        return cls(
            snapshot_id=row["snapshot_id"], vendor=row["vendor"], display_name=row["display_name"],
            fetched_at=row["fetched_at"], is_demo_mirror=_truthy(row["is_demo_mirror"]),
            source_urls=json.loads(row["source_urls"]),
            subprocessors=json.loads(row["subprocessors_json"]), policy_text=row["policy_text"],
        )


class Finding(BaseModel):
    rule: RuleId
    summary: str
    evidence_snapshot_id: str
    opened_tick: int


class StateCard(BaseModel):
    """The agent's entire memory of one vendor. Rewritten every tick, never appended to.

    Everything above `policy_paragraph_hashes` is what the model sees; the hashes are
    bookkeeping for cheap change detection and never go into a prompt.
    """
    vendor: str
    display_name: str
    tick: int = 0
    status: Literal["green", "red"] = "green"
    is_demo_mirror: bool = False
    subprocessors: list[Subprocessor] = []
    training_on_customer_data: bool | None = None   # None = unknown
    retention_days: int | None = None
    open_findings: list[Finding] = []
    notes: str = ""                  # the agent's own short working notes (kept under ~600 chars)
    last_material_change_tick: int | None = None
    last_snapshot_id: str = ""
    updated_at: datetime = Field(default_factory=now)
    policy_paragraph_hashes: list[str] = []

    def prompt_view(self) -> dict:
        return self.model_dump(mode="json", exclude={"policy_paragraph_hashes", "updated_at"})

    def to_row(self) -> dict:
        return {
            "vendor": self.vendor, "display_name": self.display_name, "tick": self.tick,
            "status": self.status, "is_demo_mirror": self.is_demo_mirror,
            "subprocessors_json": json.dumps([s.model_dump() for s in self.subprocessors]),
            "open_findings_json": json.dumps([f.model_dump() for f in self.open_findings]),
            "card_json": self.model_dump_json(), "updated_at": self.updated_at.isoformat(),
        }

    @classmethod
    def from_row(cls, row: dict) -> "StateCard":
        return cls.model_validate_json(row["card_json"])


class Alert(BaseModel):
    """A compliance change. kind=violation turns the vendor red; kind=resolved turns it green."""
    vendor: str
    display_name: str
    tick: int
    kind: Literal["violation", "resolved"] = "violation"
    rule: RuleId
    title: str                       # "Tinybird (demo mirror) added DataHarvest Ltd (Singapore)"
    before: str
    after: str
    explanation: str
    evidence_snapshot_id: str
    evidence_url: str = ""
    is_demo_mirror: bool = False
    created_at: datetime = Field(default_factory=now)
    alert_id: str = ""

    def model_post_init(self, _ctx) -> None:
        if not self.alert_id:
            self.alert_id = f"{self.vendor}:{self.tick}:{self.rule}:{self.kind}"

    def to_row(self) -> dict:
        return self.model_dump(mode="json")


class TickLog(BaseModel):
    """One agent step on one vendor. Feeds the tokens-per-tick chart."""
    run_id: str
    tick: int
    agent: Literal["nights_watch", "naive"]
    vendor: str
    input_tokens: int
    output_tokens: int = 0
    material: bool = False
    llm_calls: int = 0
    latency_ms: int = 0
    created_at: datetime = Field(default_factory=now)

    def to_row(self) -> dict:
        return self.model_dump(mode="json")
