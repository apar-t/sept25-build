"""Deterministic parts of the agent: country normalization, R1-R3 checks, policy paragraph diffing.

The LLM only extracts facts from changed text. Deciding compliance is plain code, so the
red/green verdict on stage never depends on a small model's mood.
"""

import re

from ..contracts import APPROVED_COUNTRIES, MAX_RETENTION_DAYS, Finding, StateCard, Subprocessor, sha

_ALIASES = {
    "us": "United States", "usa": "United States", "u.s.": "United States", "u.s.a.": "United States",
    "united states of america": "United States", "america": "United States",
    "uk": "United Kingdom", "u.k.": "United Kingdom", "great britain": "United Kingdom",
    "england": "United Kingdom", "scotland": "United Kingdom", "wales": "United Kingdom",
    "czechia": "Czech Republic", "the netherlands": "Netherlands", "holland": "Netherlands",
    "deutschland": "Germany",
}
# Region words that mean "somewhere in the EU/EEA": approved by definition.
_APPROVED_REGIONS = {"eu", "eea", "european union", "european economic area", "europe"}
_APPROVED = {c.lower() for c in APPROVED_COUNTRIES}


def countries(raw: str) -> list[str]:
    """'US, Ireland and EU' -> ['United States', 'Ireland', 'EU']. Empty if nothing listed."""
    parts = re.split(r",|;|/|\band\b|\bor\b|&|\(|\)", raw or "")
    out = []
    for p in parts:
        p = p.strip().strip(".").strip()
        if not p:
            continue
        key = p.lower()
        if key in _APPROVED_REGIONS:
            out.append("EU")
        else:
            out.append(_ALIASES.get(key) or (p.title() if p.islower() else p))
    return out


def unapproved(sub: Subprocessor) -> list[str]:
    """Countries of this sub-processor that are not on the approved list. Unknown country = not flagged."""
    return [c for c in countries(sub.country) if c != "EU" and c.lower() not in _APPROVED]


def sub_key(sub: Subprocessor) -> str:
    return re.sub(r"[^a-z0-9]", "", sub.name.lower())


def paragraphs(text: str) -> list[str]:
    return [re.sub(r"\s+", " ", p).strip() for p in re.split(r"\n\s*\n", text or "") if p.strip()]


def para_hash(p: str) -> str:
    return sha(p.lower())


_RELEVANT = re.compile(
    r"train|machine learning|\bai\b|artificial intelligence|\bmodels?\b|retain|retention|delet|"
    r"\b\d+\s*(day|month|year)s?\b", re.I)


def relevant(paras: list[str], limit_chars: int = 6000) -> list[str]:
    """Paragraphs that could mention training or retention, capped so the prompt stays small."""
    out, n = [], 0
    for p in paras:
        if _RELEVANT.search(p) and n + len(p) <= limit_chars:
            out.append(p)
            n += len(p)
    return out


def findings(card: StateCard, snapshot_id: str, tick: int, previous: list[Finding]) -> list[Finding]:
    """Recompute open findings from the card's current facts. Keeps opened_tick for findings still open."""
    opened = {(f.rule, f.summary): f for f in previous}
    now = []
    for s in card.subprocessors:
        bad = unapproved(s)
        if bad:
            now.append(("R1", f"{s.name} processes data in {', '.join(bad)} (not approved)"))
    if card.training_on_customer_data is True:
        now.append(("R2", "Vendor terms allow training AI models on customer data"))
    if card.retention_days is not None and card.retention_days > MAX_RETENTION_DAYS:
        now.append(("R3", f"Retention is {card.retention_days} days (max {MAX_RETENTION_DAYS})"))
    return [opened.get(k) or Finding(rule=k[0], summary=k[1], evidence_snapshot_id=snapshot_id, opened_tick=tick)
            for k in now]


_NEGATION = re.compile(r"\b(not|never|no|don't|do not|does not|won't|will not)\b", re.I)
_DURATION = re.compile(r"(\d+)\s*(day|week|month|year)s?", re.I)
_UNIT_DAYS = {"day": 1, "week": 7, "month": 30, "year": 365}


def to_days(value: float | int | None, unit: str | None) -> int | None:
    if value is None or not unit:
        return None
    return int(round(float(value) * _UNIT_DAYS.get(unit.lower().rstrip("s"), 1)))


def heuristic_facts(paras: list[str]) -> dict:
    """Regex fallback used when the LLM is unavailable or returns junk."""
    facts = {"training_on_customer_data": None, "training_quote": "", "retention_days": None, "retention_quote": ""}
    for p in paras:
        for sent in re.split(r"(?<=[.!?])\s+", p):
            low = sent.lower()
            if "train" in low and facts["training_quote"] == "":
                facts["training_on_customer_data"] = not _NEGATION.search(sent)
                facts["training_quote"] = sent
            if re.search(r"retain|retention|delet", low) and facts["retention_quote"] == "":
                m = _DURATION.search(sent)
                if m:
                    facts["retention_days"] = to_days(int(m.group(1)), m.group(2))
                    facts["retention_quote"] = sent
    return facts
