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
    "memory_ops": "nw_memory_ops",   # journal of the agent's edits to its own working memory
    "lease": "nw_agent_lease",       # single-runner lock + heartbeat
    "commits": "nw_commits",         # one row per committed batch of state cards (crash-safe commit point)
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

# The vendors we watch, and names they appear under in OTHER vendors' sub-processor lists.
# A sub-processor matching one of these is linked to that vendor's node instead of being crawled
# ("link, don't crawl"): no recursive fetching, and each company is exactly one node, so cycles
# (Tinybird -> AWS -> ...) can't loop. Add aliases as real lists show up.
VENDORS = {"aws": "AWS", "nimble": "Nimble", "tinybird": "Tinybird", "liquid": "Liquid AI", "bfl": "Black Forest Labs"}
VENDOR_ALIASES = {
    "aws": ["amazon web services", "aws", "amazon.com"],
    "nimble": ["nimble way", "nimbleway", "nimble"],
    "tinybird": ["tinybird"],
    "liquid": ["liquid ai"],
    "bfl": ["black forest labs"],
}

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

    prompt_view() is the working context. `policy_paragraph_hashes` and `ledger` are compact
    long-term memory: bookkeeping for change detection and recurrence, never put in a prompt.
    """
    vendor: str
    display_name: str
    tick: int = 0
    status: Literal["green", "red"] = "green"
    is_demo_mirror: bool = False
    subprocessors: list[Subprocessor] = []
    training_on_customer_data: bool | None = None   # None = unknown
    retention_days: int | None = None
    clause_quotes: dict[str, str] = {}   # {"training": "...", "retention": "..."}: short evidence quotes
    open_findings: list[Finding] = []
    notes: str = ""                  # the agent's own short working notes (kept under ~600 chars)
    last_material_change_tick: int | None = None
    last_snapshot_id: str = ""
    counters: dict[str, int] = {}    # ticks, unchanged, noise, material, llm_calls, violations, resolved
    check_every: int = 1             # suggested ticks between checks, from how often this vendor changes
    updated_at: datetime = Field(default_factory=now)
    policy_paragraph_hashes: list[str] = []
    ledger: dict[str, dict] = {}     # every sub-processor ever seen: first_seen, times_added, last_removed
    sentence_verdicts: dict[str, dict] = {}  # judged policy sentences still in the terms: kind, text, allows/days, by
    depends_on: list[str] = []       # watched vendors that appear in this vendor's sub-processor list
    exposed_via: list[str] = []      # red vendors reachable through depends_on (fourth-party risk)

    def prompt_view(self) -> dict:
        return self.model_dump(mode="json", exclude={"policy_paragraph_hashes", "ledger", "sentence_verdicts",
                                                     "updated_at"})

    def to_row(self) -> dict:
        return {
            "vendor": self.vendor, "display_name": self.display_name, "tick": self.tick,
            "status": self.status, "is_demo_mirror": self.is_demo_mirror,
            "subprocessors_json": json.dumps([s.model_dump() for s in self.subprocessors]),
            "open_findings_json": json.dumps([f.model_dump() for f in self.open_findings]),
            "depends_on_json": json.dumps(self.depends_on), "exposed_via_json": json.dumps(self.exposed_via),
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
    review_calls: int = 0            # second-opinion calls to the reviewer model (rare by design)
    card_tokens: int = 0             # size of the agent's working context (state card) after this step
    latency_ms: int = 0
    created_at: datetime = Field(default_factory=now)
    tick_id: str = ""                # idempotency key: a replayed tick writes the same id

    def model_post_init(self, _ctx) -> None:
        if not self.tick_id:
            self.tick_id = f"{self.run_id}:{self.tick}:{self.agent}:{self.vendor}"

    def to_row(self) -> dict:
        return self.model_dump(mode="json")


class MemoryOp(BaseModel):
    """One edit the agent made to its own working memory (the state card). The journal of these
    is how the agent's context editing becomes visible and auditable, tick by tick."""
    run_id: str
    tick: int
    vendor: str
    op: Literal["set", "open_finding", "close_finding", "remember", "forget", "compact", "keep_on_fetch_gap"]
    field: str                       # e.g. training_on_customer_data, subprocessor, sentence, notes
    before: str = ""
    after: str = ""
    why: str = ""
    created_at: datetime = Field(default_factory=now)
    op_id: str = ""                  # idempotency key: a replayed tick writes the same id

    def model_post_init(self, _ctx) -> None:
        if not self.op_id:
            self.op_id = f"{self.vendor}:{self.tick}:{self.op}:{self.field}:{sha(self.before + '>' + self.after)[:8]}"

    def to_row(self) -> dict:
        return self.model_dump(mode="json")
