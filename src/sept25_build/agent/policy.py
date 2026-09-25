"""Deterministic parts of the agent: country checks, R1-R3 findings, policy sentence detection.

The LLM only judges the meaning of individual sentences. Deciding compliance is plain code, so
the red/green verdict on stage never depends on a small model's mood.
"""

import re

from ..contracts import APPROVED_COUNTRIES, MAX_RETENTION_DAYS, Finding, StateCard, Subprocessor, sha

# R1 only fires when a country NOT on the approved list is named explicitly. Cities, regions,
# "Global", "N/A" or a missing country are never flagged: a false red on a real vendor is worse
# on stage than a missed edge case.
_NOT_APPROVED = [
    "Singapore", "India", "China", "Hong Kong", "Taiwan", "South Korea", "Korea", "Australia",
    "New Zealand", "Israel", "Philippines", "Vietnam", "Indonesia", "Malaysia", "Thailand",
    "Pakistan", "Bangladesh", "Sri Lanka", "Brazil", "Mexico", "Argentina", "Chile", "Colombia",
    "Peru", "Uruguay", "Costa Rica", "South Africa", "Nigeria", "Kenya", "Egypt", "Morocco",
    "Russia", "Ukraine", "Belarus", "Serbia", "Turkey", "Türkiye", "United Arab Emirates", "UAE",
    "Saudi Arabia", "Qatar",
]
_APPROVED_LOWER = {c.lower() for c in APPROVED_COUNTRIES}
_NOT_APPROVED_RE = [(c, re.compile(rf"(?<![a-z]){re.escape(c.lower())}(?![a-z])"))
                    for c in _NOT_APPROVED if c.lower() not in _APPROVED_LOWER]


def unapproved(sub: Subprocessor) -> list[str]:
    """Non-approved countries explicitly named in this sub-processor's location."""
    text = (sub.country or "").lower()
    found = [c for c, pat in _NOT_APPROVED_RE if pat.search(text)]
    return [c for c in found if not (c == "Korea" and "South Korea" in found)]


def sub_key(sub: Subprocessor) -> str:
    return re.sub(r"[^a-z0-9]", "", sub.name.lower())


def paragraphs(text: str) -> list[str]:
    return [re.sub(r"\s+", " ", p).strip() for p in re.split(r"\n\s*\n", text or "") if p.strip()]


def para_hash(p: str) -> str:
    return sha(p.lower())


def location(sub: Subprocessor, card: StateCard) -> str:
    """Listed country, or the one the agent established by investigation when none is listed."""
    return sub.country if sub.country.strip() else card.investigations.get(sub_key(sub), {}).get("country", "")


def findings(card: StateCard, snapshot_id: str, tick: int, previous: list[Finding]) -> list[Finding]:
    """Recompute open findings from the card's current facts. Keeps opened_tick for findings still open."""
    opened = {(f.rule, f.summary): f for f in previous}
    now = []
    for s in card.subprocessors:
        bad = unapproved(Subprocessor(name=s.name, country=location(s, card)))
        if bad and ("R1", f"{s.name} processes data in {', '.join(bad)} (not approved)") not in now:
            now.append(("R1", f"{s.name} processes data in {', '.join(bad)} (not approved)"))
    if card.training_on_customer_data is True:
        now.append(("R2", "Vendor terms allow training AI models on customer data"))
    if card.retention_days is not None and card.retention_days > MAX_RETENTION_DAYS:
        now.append(("R3", f"Retention is {card.retention_days} days (max {MAX_RETENTION_DAYS})"))
    return [opened.get(k) or Finding(rule=k[0], summary=k[1], evidence_snapshot_id=snapshot_id, opened_tick=tick)
            for k in now]


# ---- policy sentences -------------------------------------------------------------------------

_NEGATION = re.compile(r"\b(not|never|no|don't|do not|does not|won't|will not|without)\b", re.I)
_CLAUSE_SPLIT = re.compile(r"\b(?:but|however|although|though|whereas|except)\b|;", re.I)
_DURATION = re.compile(r"(\d+)\s*(day|week|month|year)s?", re.I)
_UNIT_DAYS = {"day": 1, "week": 7, "month": 30, "year": 365}
# "train" only counts near model words, so "we train our staff" doesn't turn a vendor red
_TRAINING = re.compile(r"\btrain\w*\b.{0,80}\b(models?|machine learning|ml|ai|artificial intelligence|algorithms?)\b|"
                       r"\b(models?|machine learning|ml|ai|artificial intelligence)\b.{0,40}\btrain", re.I)
# ...and only when the sentence is about the customer's data, not the vendor's own training data
_CUSTOMER_DATA = re.compile(
    r"\b(customer|your|user|client|personal|end[- ]user)s?'?\s+(data|content|information|inputs?|prompts?|files|materials)\b|"
    r"\bcontent (?:you|that you) (?:submit|provide|upload)|\bdata (?:you|that you) (?:submit|provide|upload)", re.I)
_RETENTION = re.compile(r"retain|retention|delet|stored? for|kept for|keep", re.I)
# R3 is about customer data, not tax records, invoices or legal holds
_RETENTION_EXCLUDE = re.compile(r"\b(tax|invoice|billing|accounting|financial records?|legal (?:hold|obligation|requirement)s?|audit)\b", re.I)


def norm(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", re.sub(r"\s+", " ", text.lower())).strip()


def sentences(paragraph: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+", paragraph) if s.strip()]


def sentence_key(kind: str, sentence: str) -> str:
    return f"{kind}:{sha(norm(sentence))}"


def to_days(value: float | int | None, unit: str | None) -> int | None:
    if value is None or not unit:
        return None
    return int(round(float(value) * _UNIT_DAYS.get(unit.lower().rstrip("s"), 1)))


def duration_days(sentence: str) -> int | None:
    """Longest duration mentioned in a sentence, in days. "30 days ... up to 24 months" -> 720."""
    found = [to_days(int(n), u) for n, u in _DURATION.findall(sentence)]
    return max(found) if found else None


def candidates(paras: list[str]) -> list[tuple[str, str]]:
    """Every (kind, sentence) in the whole policy that could matter for R2 or R3. Deduplicated."""
    out, seen = [], set()
    for p in paras:
        for s in sentences(p):
            if not _CUSTOMER_DATA.search(s):
                continue
            kinds = []
            if _TRAINING.search(s):
                kinds.append("training")
            if _RETENTION.search(s) and _DURATION.search(s) and not _RETENTION_EXCLUDE.search(s):
                kinds.append("retention")
            for k in kinds:
                if sentence_key(k, s) not in seen:
                    seen.add(sentence_key(k, s))
                    out.append((k, s))
    return out


def trains(sentence: str) -> bool:
    """Regex polarity: look only at the clause that mentions training, so
    "we never sell your data but may use it to train our models" reads as allowing training."""
    clauses = [c for c in _CLAUSE_SPLIT.split(sentence) if c and re.search(r"train", c, re.I)] or [sentence]
    return any(not _NEGATION.search(c) for c in clauses)
