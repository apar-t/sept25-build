"""One agent step: (state card, new snapshot) -> (rewritten card, alerts, tick log).

The model's context per step is the card's facts plus the changed text only, so it stays
the same size on tick 5 and tick 5,000. Full snapshots live in RawTree, never in a prompt.
"""

import json
import time

from ..contracts import COMPANY, RULES, Alert, Snapshot, StateCard, Subprocessor, TickLog
from . import llm, policy

SYSTEM = (
    "You read changes to a vendor's data-processing terms and extract two facts. "
    "Answer only from the text given. If the text does not mention a fact, return null for it."
)
FACTS_SCHEMA = {
    "type": "object",
    "properties": {
        "training_on_customer_data": {"type": ["boolean", "null"],
                                      "description": "true if the vendor may use customer data to train AI/ML models"},
        "training_quote": {"type": "string", "description": "exact sentence supporting it, or empty"},
        "retention_value": {"type": ["number", "null"]},
        "retention_unit": {"type": ["string", "null"], "enum": ["day", "week", "month", "year", None]},
        "retention_quote": {"type": "string", "description": "exact sentence supporting it, or empty"},
        "is_material": {"type": "boolean",
                        "description": "false if the change is cosmetic (dates, formatting, wording with the same meaning)"},
        "reason": {"type": "string", "description": "one short sentence"},
    },
    "required": ["training_on_customer_data", "training_quote", "retention_value", "retention_unit",
                 "retention_quote", "is_material", "reason"],
}
NOTES_MAX = 600


def _extract(card: StateCard, changed: list[str]) -> tuple[dict, dict, str]:
    """Ask Liquid what the changed text means. Falls back to regexes if the model is unavailable."""
    known = {"training_on_customer_data": card.training_on_customer_data,
             "retention_days": card.retention_days, "quotes": card.clause_quotes}
    user = (f"Facts currently on file for {card.display_name}:\n{json.dumps(known)}\n\n"
            "New or changed paragraphs in their terms:\n" + "\n\n".join(f"- {p}" for p in changed))
    if llm.available():
        for _ in range(2):
            try:
                out, usage = llm.json_call(SYSTEM, user, FACTS_SCHEMA)
                facts = {
                    "training_on_customer_data": out.get("training_on_customer_data"),
                    "training_quote": out.get("training_quote") or "",
                    "retention_days": policy.to_days(out.get("retention_value"), out.get("retention_unit")),
                    "retention_quote": out.get("retention_quote") or "",
                    "reason": out.get("reason") or "",
                }
                return facts, usage, "liquid"
            except (ValueError, json.JSONDecodeError):
                continue
    return policy.heuristic_facts(changed) | {"reason": "regex fallback"}, {"input_tokens": 0, "output_tokens": 0}, "regex"


def _alert(card: StateCard, snap: Snapshot, tick: int, kind: str, rule: str, title: str,
           before: str, after: str, explanation: str) -> Alert:
    return Alert(vendor=card.vendor, display_name=card.display_name, tick=tick, kind=kind, rule=rule,
                 title=title, before=before, after=after, explanation=explanation,
                 evidence_snapshot_id=snap.snapshot_id, evidence_url=(snap.source_urls or [""])[0],
                 is_demo_mirror=snap.is_demo_mirror)


def _sub_alerts(old: StateCard, new: StateCard, snap: Snapshot, tick: int) -> list[Alert]:
    alerts = []
    before = {policy.sub_key(s): s for s in old.subprocessors}
    after = {policy.sub_key(s): s for s in new.subprocessors}
    for k, s in after.items():
        bad, was_bad = policy.unapproved(s), policy.unapproved(before[k]) if k in before else []
        if bad and not was_bad:
            verb = "added" if k not in before else "moved"
            alerts.append(_alert(new, snap, tick, "violation", "R1",
                                 f"{new.display_name} {verb} {s.name} ({s.country})",
                                 "not listed" if k not in before else f"{before[k].name}: {before[k].country}",
                                 f"{s.name}: {s.purpose} ({s.country})",
                                 f"{s.name} processes data in {', '.join(bad)}, which is not on {COMPANY}'s "
                                 f"approved list. {RULES['R1']}."))
    for k, s in before.items():
        if policy.unapproved(s) and (k not in after or not policy.unapproved(after[k])):
            alerts.append(_alert(new, snap, tick, "resolved", "R1",
                                 f"{new.display_name} no longer sends data to {s.name} ({s.country})",
                                 f"{s.name}: {s.country}", "removed" if k not in after else after[k].country,
                                 f"{RULES['R1']}: back in compliance."))
    return alerts


def _clause_alerts(old: StateCard, new: StateCard, snap: Snapshot, tick: int) -> list[Alert]:
    alerts = []
    q_old, q_new = old.clause_quotes, new.clause_quotes
    was, now = old.training_on_customer_data is True, new.training_on_customer_data is True
    if now != was:
        alerts.append(_alert(new, snap, tick, "violation" if now else "resolved", "R2",
                             f"{new.display_name} {'now allows' if now else 'no longer allows'} training on customer data",
                             q_old.get("training", "not stated"), q_new.get("training", "not stated"),
                             f"{RULES['R2']}." + (" This change breaks it." if now else " Back in compliance.")))
    limit = policy.MAX_RETENTION_DAYS
    was = old.retention_days is not None and old.retention_days > limit
    now = new.retention_days is not None and new.retention_days > limit
    if now != was:
        alerts.append(_alert(new, snap, tick, "violation" if now else "resolved", "R3",
                             f"{new.display_name} retention changed to {new.retention_days} days",
                             q_old.get("retention", f"{old.retention_days} days"),
                             q_new.get("retention", f"{new.retention_days} days"),
                             f"{RULES['R3']}." + (" This change breaks it." if now else " Back in compliance.")))
    return alerts


def step(card: StateCard | None, snap: Snapshot, tick: int, run_id: str = "live") -> tuple[StateCard, list[Alert], TickLog]:
    t0 = time.monotonic()
    first = card is None
    old = card or StateCard(vendor=snap.vendor, display_name=snap.display_name, is_demo_mirror=snap.is_demo_mirror)
    new = old.model_copy(deep=True)
    new.tick, new.last_snapshot_id, new.display_name = tick, snap.snapshot_id, snap.display_name
    new.updated_at = snap.fetched_at

    # 1. Sub-processors: structured, so diff them in code.
    new.subprocessors = [Subprocessor(**s.model_dump()) for s in snap.subprocessors]
    subs_changed = ({(policy.sub_key(s), s.country) for s in old.subprocessors}
                    != {(policy.sub_key(s), s.country) for s in new.subprocessors})

    # 2. Policy text: only paragraphs whose hash we haven't seen go to the model. Unchanged = 0 tokens.
    paras = snap.policy_text and policy.paragraphs(snap.policy_text) or []
    seen = set(old.policy_paragraph_hashes)
    changed = policy.relevant(paras) if first else [p for p in paras if policy.para_hash(p) not in seen]
    usage, source, reason = {"input_tokens": 0, "output_tokens": 0}, "none", ""
    if changed:
        facts, usage, source = _extract(old, changed)
        reason = facts.get("reason", "")
        if facts["training_on_customer_data"] is not None:
            new.training_on_customer_data = facts["training_on_customer_data"]
            if facts["training_quote"]:
                new.clause_quotes["training"] = facts["training_quote"][:300]
        if facts["retention_days"] is not None:
            new.retention_days = facts["retention_days"]
            if facts["retention_quote"]:
                new.clause_quotes["retention"] = facts["retention_quote"][:300]
    if paras:
        new.policy_paragraph_hashes = [policy.para_hash(p) for p in paras]

    # 3. Compliance is decided in code, from the rewritten card.
    new.open_findings = policy.findings(new, snap.snapshot_id, tick, old.open_findings)
    new.status = "red" if new.open_findings else "green"
    facts_changed = (old.training_on_customer_data, old.retention_days) != (new.training_on_customer_data, new.retention_days)
    material = first or subs_changed or facts_changed
    alerts = _sub_alerts(old, new, snap, tick) + _clause_alerts(old, new, snap, tick)
    if material:
        new.last_material_change_tick = tick

    # 4. The agent's own working notes: newest first, oldest lines fall off at NOTES_MAX.
    if material or changed:
        line = (f"t{tick}: " + ("baseline. " if first else "") +
                ("; ".join(a.title for a in alerts) if alerts
                 else "material change, still compliant" if material else f"noise discarded ({reason or 'no change'})"))
        notes = (line + "\n" + old.notes).strip()
        new.notes = notes if len(notes) <= NOTES_MAX else notes[:NOTES_MAX].rsplit("\n", 1)[0]

    log = TickLog(run_id=run_id, tick=tick, agent="nights_watch", vendor=snap.vendor,
                  input_tokens=usage["input_tokens"], output_tokens=usage["output_tokens"],
                  material=material, llm_calls=int(source == "liquid"),
                  latency_ms=int((time.monotonic() - t0) * 1000))
    return new, alerts, log


def naive_tokens(prev_total: int, snap: Snapshot, first: bool) -> int:
    """Context a naive agent would carry: every snapshot so far, appended to its history.

    Measured with the same tokenizer, not run (running it would just be slow). Label it that way.
    """
    body = json.dumps({"subprocessors": [s.model_dump() for s in snap.subprocessors],
                       "policy_text": snap.policy_text})
    overhead = 150 if first else 0  # system prompt + instructions, paid once
    return prev_total + overhead + llm.count_tokens(body)
