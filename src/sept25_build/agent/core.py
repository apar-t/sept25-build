"""One agent step: (state card, new snapshot) -> (rewritten card, alerts, tick log).

The model's context per step is the card's facts plus the changed text only, so it stays
the same size on tick 5 and tick 5,000. Full snapshots live in RawTree, never in a prompt.
"""

import json
import time

from ..contracts import COMPANY, RULES, Alert, MemoryOp, Snapshot, StateCard, Subprocessor, TickLog, sha
from . import llm, policy

SYSTEM = (
    "You judge numbered sentences from a vendor's data-processing terms. For each sentence answer:\n"
    "- allows_training: true if it says the vendor MAY use customer data to train AI/ML models, "
    "false if it says it will NOT, null if it is not about training on customer data.\n"
    "- customer_data_retention: true if it states how long CUSTOMER data is kept or when it is deleted, "
    "false otherwise (tax records, logs, invoices, notice periods are false).\n"
    "Judge each sentence on its own words only."
)
VERDICT_SCHEMA = {
    "type": "object",
    "properties": {"items": {"type": "array", "items": {
        "type": "object",
        "properties": {"i": {"type": "integer"},
                       "allows_training": {"type": ["boolean", "null"]},
                       "customer_data_retention": {"type": "boolean"}},
        "required": ["i", "allows_training", "customer_data_retention"],
        "additionalProperties": False}}},
    "required": ["items"],
    "additionalProperties": False,
}
LLM_BATCH = 12   # most new sentences judged per call; the rest use the regex verdict
REVIEW_MAX = 8   # most sentences escalated to the reviewer model per step
NOTES_MAX = 700
NOTES_KEEP = 5


def _judge(new_sents: list[tuple[str, str]]) -> tuple[dict[str, dict], dict, str]:
    """Verdicts for policy sentences the agent hasn't seen before.

    Division of labour, because a 2.6B model echoes context and fumbles units:
      - regex decides WHICH sentences can matter (policy.candidates); no candidate, no call
      - Liquid decides what each one means, answering by sentence number (nothing to copy or invent)
      - regex polarity is the fallback per sentence; retention days are always parsed in code
    """
    usage, source, out = {"input_tokens": 0, "output_tokens": 0}, "regex", {}
    batch = new_sents[:LLM_BATCH]
    if batch and llm.available():
        user = "\n".join(f"{i}. {s}" for i, (_, s) in enumerate(batch))
        for _ in range(2):
            try:
                raw, usage = llm.json_call(SYSTEM, user, VERDICT_SCHEMA)
                out = {int(it["i"]): it for it in raw.get("items", []) if isinstance(it, dict) and "i" in it}
                source = "liquid"
                break
            except (ValueError, KeyError, TypeError, json.JSONDecodeError):
                continue
            except RuntimeError:  # rate limit / timeout: don't burn more requests
                break
    verdicts = {}
    for i, (kind, sent) in enumerate(new_sents):
        v = {"kind": kind, "text": sent[:300], "by": "regex"}
        llm_v = out.get(i) if i < len(batch) else None
        if kind == "training":
            v["allows"] = policy.trains(sent)
            if llm_v and isinstance(llm_v.get("allows_training"), bool):
                v["allows"], v["by"] = llm_v["allows_training"], "liquid"
            elif llm_v and llm_v.get("allows_training") is None and "allows_training" in llm_v:
                v["allows"], v["by"] = None, "liquid"   # the model says it isn't about customer data
        else:
            v["days"] = policy.duration_days(sent)
            if llm_v and llm_v.get("customer_data_retention") is False:
                v["days"], v["by"] = None, "liquid"
            elif llm_v:
                v["by"] = "liquid"
        verdicts[policy.sentence_key(kind, sent)] = v
    usage = dict(usage, **_second_opinion(verdicts))
    return verdicts, usage, source


def _decisive(v: dict) -> bool:
    return (v["kind"] == "training" and v.get("allows") is True) or \
           (v["kind"] == "retention" and (v.get("days") or 0) > policy.MAX_RETENTION_DAYS)


def _disagrees(v: dict) -> bool:
    if v["kind"] == "training":
        return v.get("allows") != policy.trains(v["text"])
    return v.get("days") != policy.duration_days(v["text"])


def _second_opinion(verdicts: dict[str, dict]) -> dict:
    """Escalate only the sentences that would change a verdict, or where Liquid and the rules
    disagree, to the stronger reviewer model. Its answer wins; Liquid's is kept for the record."""
    todo = [v for v in verdicts.values() if _decisive(v) or _disagrees(v)][:REVIEW_MAX]
    if not todo or not llm.review_available():
        return {"review_calls": 0}
    user = "\n".join(f"{i}. {v['text']}" for i, v in enumerate(todo))
    try:
        raw, usage = llm.review_call(SYSTEM, user, VERDICT_SCHEMA)
    except (RuntimeError, ValueError, json.JSONDecodeError):
        return {"review_calls": 0}
    out = {int(it["i"]): it for it in raw.get("items", []) if isinstance(it, dict) and "i" in it}
    for i, v in enumerate(todo):
        r = out.get(i)
        if not r:
            continue
        before = v.get("allows") if v["kind"] == "training" else v.get("days")
        if v["kind"] == "training":
            after = r.get("allows_training") if isinstance(r.get("allows_training"), (bool, type(None))) else before
            v["allows"] = after
        else:
            after = policy.duration_days(v["text"]) if r.get("customer_data_retention") else None
            v["days"] = after
        v["first_opinion"] = {"by": v["by"], "value": before}
        v["by"] = "reviewer-confirmed" if after == before else "reviewer-override"
    return {"review_calls": 1, "review_input_tokens": usage["input_tokens"],
            "review_output_tokens": usage["output_tokens"]}


def _who_judged(v: dict | None) -> str:
    """One line for the alert: which model (or rule) made the call."""
    liquid, reviewer = llm.MODEL.split("/")[-1].removesuffix(":free"), llm.REVIEW_MODEL.split("/")[-1]
    by = (v or {}).get("by", "")
    first = liquid if (v or {}).get("first_opinion", {}).get("by") == "liquid" else "the rules"
    return {"liquid": f"Judged by {liquid}.",
            "regex": "Judged by rules (LLM unavailable).",
            "reviewer-confirmed": f"Flagged by {first}; confirmed by {reviewer}.",
            "reviewer-override": f"{reviewer} overrode {first}'s reading."}.get(by, "")


def _facts(verdicts: dict[str, dict]) -> tuple[bool | None, str, int | None, str]:
    """Current facts from every judged sentence still in the terms. Removed sentences are gone
    from `verdicts`, so deleting an injected clause turns the vendor green again."""
    t = [v for v in verdicts.values() if v["kind"] == "training" and v.get("allows") is not None]
    r = [v for v in verdicts.values() if v["kind"] == "retention" and v.get("days") is not None]
    allowing = [v for v in t if v["allows"]]
    training = True if allowing else False if t else None
    t_quote = (allowing or t or [{"text": ""}])[0]["text"]
    longest = max(r, key=lambda v: v["days"]) if r else None
    return training, t_quote, (longest["days"] if longest else None), (longest["text"] if longest else "")


def _alert(card: StateCard, snap: Snapshot, tick: int, kind: str, rule: str, title: str,
           before: str, after: str, explanation: str) -> Alert:
    return Alert(vendor=card.vendor, display_name=card.display_name, tick=tick, kind=kind, rule=rule,
                 title=title, before=before, after=after, explanation=explanation,
                 evidence_snapshot_id=snap.snapshot_id, evidence_url=(snap.source_urls or [""])[0],
                 is_demo_mirror=snap.is_demo_mirror,
                 alert_id=f"{card.vendor}:{tick}:{rule}:{kind}:{sha(title)[:8]}")


def _ordinal(n: int) -> str:
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def _update_ledger(old: StateCard, new: StateCard, tick: int) -> None:
    """Compact long-term memory of every sub-processor ever seen. O(distinct names), not O(ticks)."""
    before = {policy.sub_key(s) for s in old.subprocessors}
    after = {policy.sub_key(s): s for s in new.subprocessors}
    ledger = {k: dict(v) for k, v in old.ledger.items()}
    for k, s in after.items():
        if k not in before:
            e = ledger.setdefault(k, {"name": s.name, "first_seen": tick, "times_added": 0, "last_removed": None})
            e["times_added"] += 1
        ledger[k]["country"] = s.country
    for k in before - after.keys():
        if k in ledger:
            ledger[k]["last_removed"] = tick
    new.ledger = ledger


def _compact_notes(old_notes: str, line: str, c: dict) -> str:
    """The agent edits its own working notes: keep the last NOTES_KEEP events verbatim and fold
    everything older into one summary line built from the counters. Size stays bounded forever."""
    recent = [line] + [n for n in old_notes.splitlines() if n and not n.startswith("earlier:")]
    kept = recent[:NOTES_KEEP]
    if len(recent) > NOTES_KEEP or any(n.startswith("earlier:") for n in old_notes.splitlines()):
        kept.append(f"earlier (totals so far): {c.get('ticks', 0)} ticks watched, {c.get('noise', 0) + c.get('unchanged', 0)} "
                    f"discarded, {c.get('violations', 0)} violations, {c.get('resolved', 0)} resolved")
    return "\n".join(kept)[:NOTES_MAX]


def _by_name(subs: list[Subprocessor]) -> dict[str, dict]:
    """Group rows by sub-processor name, so a vendor listing one company in several regions is one entry."""
    out: dict[str, dict] = {}
    for sp in subs:
        e = out.setdefault(policy.sub_key(sp), {"name": sp.name, "purpose": sp.purpose, "locations": [], "bad": []})
        e["locations"].append(sp.country)
        e["bad"] += [c for c in policy.unapproved(sp) if c not in e["bad"]]
    return out


def _sub_alerts(old: StateCard, new: StateCard, snap: Snapshot, tick: int, first: bool) -> list[Alert]:
    alerts = []
    before, after = _by_name(old.subprocessors), _by_name(new.subprocessors)
    for k, e in after.items():
        fresh_bad = [c for c in e["bad"] if c not in before.get(k, {}).get("bad", [])]
        if not fresh_bad:
            continue
        where = ", ".join(x for x in e["locations"] if x) or ", ".join(fresh_bad)
        past = old.ledger.get(k, {})
        explanation = (f"{e['name']} processes data in {', '.join(fresh_bad)}, which is not on {COMPANY}'s "
                       f"approved list. {RULES['R1']}.")
        if first:
            title = f"Baseline: {new.display_name} already uses {e['name']} ({where})"
        elif k in before:
            title = f"{new.display_name} moved {e['name']} to {where}"
        elif past.get("last_removed") is not None:
            title = f"{new.display_name} re-added {e['name']} ({where}), {_ordinal(past.get('times_added', 1) + 1)} time"
            explanation += (f" Recurring: first seen t{past.get('first_seen')}, last removed "
                            f"t{past['last_removed']}. Escalate: removal didn't stick.")
        else:
            title = f"{new.display_name} added {e['name']} ({where})"
        alerts.append(_alert(new, snap, tick, "violation", "R1", title,
                             "not listed" if k not in before else ", ".join(before[k]["locations"]),
                             f"{e['name']}: {e['purpose']} ({where})", explanation))
    still_open = any(f.rule == "R1" for f in new.open_findings)
    for k, e in before.items():
        gone_bad = [c for c in e["bad"] if c not in after.get(k, {}).get("bad", [])]
        if not gone_bad:
            continue
        alerts.append(_alert(new, snap, tick, "resolved", "R1",
                             f"{new.display_name} no longer sends data to {e['name']} in {', '.join(gone_bad)}",
                             ", ".join(e["locations"]), "removed" if k not in after else ", ".join(after[k]["locations"]),
                             f"{e['name']} no longer processes data in {', '.join(gone_bad)}. "
                             + ("Other R1 findings are still open." if still_open else "Back in compliance with R1.")))
    return alerts


def _clause_alerts(old: StateCard, new: StateCard, snap: Snapshot, tick: int, first: bool) -> list[Alert]:
    alerts = []
    q_old, q_new = old.clause_quotes, new.clause_quotes
    was, now = old.training_on_customer_data is True, new.training_on_customer_data is True
    if now != was:
        title = (f"Baseline: {new.display_name}'s terms allow training on customer data" if first else
                 f"{new.display_name} {'now allows' if now else 'no longer allows'} training on customer data")
        alerts.append(_alert(new, snap, tick, "violation" if now else "resolved", "R2", title,
                             q_old.get("training", "not stated"), q_new.get("training", "not stated"),
                             f"{RULES['R2']}." + (" This change breaks it." if now and not first else
                                                  " Back in compliance." if not now else "")))
    limit = policy.MAX_RETENTION_DAYS
    was = old.retention_days is not None and old.retention_days > limit
    now = new.retention_days is not None and new.retention_days > limit
    if now != was:
        title = (f"Baseline: {new.display_name} keeps customer data {new.retention_days} days" if first else
                 f"{new.display_name} retention changed to {new.retention_days} days" if new.retention_days
                 else f"{new.display_name} no longer states a retention period over {limit} days")
        alerts.append(_alert(new, snap, tick, "violation" if now else "resolved", "R3", title,
                             q_old.get("retention", f"{old.retention_days} days"),
                             q_new.get("retention", f"{new.retention_days} days"),
                             f"{RULES['R3']}." + (" This change breaks it." if now and not first else
                                                  " Back in compliance." if not now else "")))
    return alerts


def step(card: StateCard | None, snap: Snapshot, tick: int, run_id: str = "live") -> tuple[StateCard, list[Alert], TickLog]:
    t0 = time.monotonic()
    first = card is None
    old = card or StateCard(vendor=snap.vendor, display_name=snap.display_name, is_demo_mirror=snap.is_demo_mirror)
    new = old.model_copy(deep=True)
    new.tick, new.last_snapshot_id, new.display_name = tick, snap.snapshot_id, snap.display_name
    new.updated_at = snap.fetched_at
    c = dict(old.counters)

    # 1. Sub-processors: structured, so diff them in code. An empty list after a non-empty one
    #    is almost always a failed fetch, not a vendor dropping every sub-processor: keep the old list.
    if snap.subprocessors or first or not old.subprocessors:
        new.subprocessors = [Subprocessor(**sp.model_dump()) for sp in snap.subprocessors]
    else:
        c["fetch_gaps"] = c.get("fetch_gaps", 0) + 1
    subs_changed = ({(policy.sub_key(sp), sp.country) for sp in old.subprocessors}
                    != {(policy.sub_key(sp), sp.country) for sp in new.subprocessors})

    # 2. Policy text. Unchanged paragraphs cost nothing. If anything changed, find the candidate
    #    sentences in the whole policy; only ones never judged before go to Liquid.
    paras = policy.paragraphs(snap.policy_text)
    hashes = [policy.para_hash(p) for p in paras]
    usage, source = {"input_tokens": 0, "output_tokens": 0}, "none"
    text_changed = bool(paras) and (first or set(hashes) != set(old.policy_paragraph_hashes))
    if not paras and old.policy_paragraph_hashes:
        c["fetch_gaps"] = c.get("fetch_gaps", 0) + 1           # empty policy = failed fetch: keep facts
    elif text_changed:
        cands = policy.candidates(paras)
        keys = [policy.sentence_key(k, sent) for k, sent in cands]
        new_sents = [cs for cs, key in zip(cands, keys) if key not in old.sentence_verdicts]
        judged, usage, source = _judge(new_sents) if new_sents else ({}, usage, "gate")
        new.sentence_verdicts = {key: old.sentence_verdicts.get(key) or judged[key] for key in keys}
        (new.training_on_customer_data, t_quote, new.retention_days, r_quote) = _facts(new.sentence_verdicts)
        new.clause_quotes = {k: v for k, v in {"training": t_quote, "retention": r_quote}.items() if v}
        new.policy_paragraph_hashes = hashes

    # 3. Compliance is decided in code, from the rewritten card.
    new.open_findings = policy.findings(new, snap.snapshot_id, tick, old.open_findings)
    new.status = "red" if new.open_findings else "green"
    facts_changed = (old.training_on_customer_data, old.retention_days) != (new.training_on_customer_data, new.retention_days)
    material = first or subs_changed or facts_changed
    alerts = _sub_alerts(old, new, snap, tick, first) + _clause_alerts(old, new, snap, tick, first)
    by_text = {v["text"]: v for v in new.sentence_verdicts.values()}
    for a in alerts:
        if a.rule in ("R2", "R3"):
            quote = new.clause_quotes.get("training" if a.rule == "R2" else "retention", "")
            who = _who_judged(by_text.get(quote[:300]))
            a.explanation = f"{a.explanation} {who}".strip()
    _update_ledger(old, new, tick)
    if material:
        new.last_material_change_tick = tick

    # 4. Counters replace history: what happened is remembered as numbers, not transcripts.
    kind = "material" if material else "noise" if text_changed else "unchanged"
    for key, inc in [("ticks", 1), (kind, 1), ("llm_calls", int(source == "liquid")),
                     ("review_calls", usage.get("review_calls", 0)),
                     ("violations", sum(a.kind == "violation" for a in alerts)),
                     ("resolved", sum(a.kind == "resolved" for a in alerts))]:
        c[key] = c.get(key, 0) + inc
    new.counters = c
    quiet = tick - (new.last_material_change_tick or tick)
    new.check_every = 1 if snap.is_demo_mirror or new.open_findings or quiet < 7 else 3 if quiet < 30 else 7

    # 5. Working notes: only events worth remembering get a line; noise just bumps a counter.
    if material or alerts:
        line = (f"t{tick}: " + ("baseline. " if first else "") +
                ("; ".join(a.title for a in alerts) if alerts else "material change, still compliant"))
        new.notes = _compact_notes(old.notes, line, c)

    log = TickLog(run_id=run_id, tick=tick, agent="nights_watch", vendor=snap.vendor,
                  input_tokens=usage["input_tokens"], output_tokens=usage["output_tokens"],
                  material=material, llm_calls=int(source == "liquid"), review_calls=usage.get("review_calls", 0),
                  card_tokens=llm.count_tokens(json.dumps(new.prompt_view(), ensure_ascii=False)),
                  latency_ms=int((time.monotonic() - t0) * 1000))
    return new, alerts, log


def memory_ops(old: StateCard | None, new: StateCard, tick: int, run_id: str) -> list[MemoryOp]:
    """The edits this step made to the agent's own working memory, as an auditable journal.

    Everything the agent keeps, changes or forgets shows up here; noise never does (it only
    bumps a counter), which is the point: the journal grows with change, not with time.
    """
    def op(kind, field, before="", after="", why=""):
        return MemoryOp(run_id=run_id, tick=tick, vendor=new.vendor, op=kind, field=field,
                        before=str(before)[:200], after=str(after)[:200], why=str(why)[:200])
    ops = []
    if old is None:
        return [op("remember", "baseline", after=f"{len(new.subprocessors)} sub-processors, "
                   f"{len(new.sentence_verdicts)} policy sentences judged", why="first observation")]
    for field in ("training_on_customer_data", "retention_days", "status"):
        a, b = getattr(old, field), getattr(new, field)
        if a != b:
            why = new.clause_quotes.get("training" if field.startswith("training") else "retention", "") \
                if field != "status" else "; ".join(f.summary for f in new.open_findings)
            ops.append(op("set", field, a, b, why))
    subs_old = {policy.sub_key(sp): sp for sp in old.subprocessors}
    subs_new = {policy.sub_key(sp): sp for sp in new.subprocessors}
    ops += [op("remember", "subprocessor", after=f"{sp.name} ({sp.country})") for k, sp in subs_new.items() if k not in subs_old]
    ops += [op("forget", "subprocessor", before=f"{sp.name} ({sp.country})", why="no longer listed; kept in ledger")
            for k, sp in subs_old.items() if k not in subs_new]
    ops += [op("remember", "sentence", after=v["text"], why=f"{v['kind']} verdict by {v['by']}")
            for k, v in new.sentence_verdicts.items() if k not in old.sentence_verdicts]
    ops += [op("forget", "sentence", before=v["text"], why="no longer in the terms")
            for k, v in old.sentence_verdicts.items() if k not in new.sentence_verdicts]
    f_old = {(f.rule, f.summary) for f in old.open_findings}
    f_new = {(f.rule, f.summary) for f in new.open_findings}
    ops += [op("open_finding", r, after=sm) for r, sm in f_new - f_old]
    ops += [op("close_finding", r, before=sm) for r, sm in f_old - f_new]
    if "earlier" in new.notes and new.notes != old.notes and len(old.notes.splitlines()) >= NOTES_KEEP:
        ops.append(op("compact", "notes", before=f"{len(old.notes)} chars", after=f"{len(new.notes)} chars",
                      why=f"kept last {NOTES_KEEP} events, folded older ones into totals"))
    if new.counters.get("fetch_gaps", 0) > old.counters.get("fetch_gaps", 0):
        ops.append(op("keep_on_fetch_gap", "snapshot", why="empty fetch: previous memory kept"))
    return ops


def naive_tokens(prev_total: int, snap: Snapshot, first: bool) -> int:
    """Context a naive agent would carry: every snapshot so far, appended to its history.

    Measured with the same tokenizer, not run (running it would just be slow). Label it that way.
    """
    body = json.dumps({"subprocessors": [s.model_dump() for s in snap.subprocessors],
                       "policy_text": snap.policy_text}, ensure_ascii=False)
    overhead = 150 if first else 0  # system prompt + instructions, paid once
    return prev_total + overhead + llm.count_tokens(body)
