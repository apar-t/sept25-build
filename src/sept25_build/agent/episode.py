"""The Night's Watch agent: a tool-using LLM that runs in short, fresh episodes.

Long-horizon shape (the hackathon theme):
  - every episode starts from the STATE CARD plus what changed this tick. No chat history, ever,
    so the context is the same size on day 1 and day 1,000
  - the agent pulls history on demand with `recall` instead of carrying it
  - it must end with `commit`, which edits its own working memory (locations it established,
    notes, check cadence, an escalation memo). Only the commit persists; the transcript is logged
    for the dashboard and thrown away
  - rules (R1-R3) stay in code: the agent adds evidence and resolves unknowns, it can't clear a
    violation the rules found

Tools: recall (RawTree/card memory), search_web + fetch_page (Nimble), judge_sentences (Liquid),
check_fourth_parties (supply-chain graph), commit.
"""

import json
import os
import re
import time

import requests
from dotenv import load_dotenv

from ..contracts import APPROVED_COUNTRIES, COMPANY, RULES, Episode, Snapshot, StateCard, Subprocessor
from . import llm, policy

load_dotenv()

MAX_TOOL_CALLS = 6
RESULT_CHARS = 2500

SYSTEM = f"""You are Night's Watch, a compliance agent watching one vendor for {COMPANY}.
Rules: R1 {RULES['R1']}. Approved: EU/EEA, {', '.join(c for c in APPROVED_COUNTRIES[-5:])}. R2 {RULES['R2']}. R3 {RULES['R3']}.
You have no chat history. Your memory is the STATE CARD below; everything older is in the store (use recall).
Investigate only what the OPEN QUESTIONS and CHANGES need, with as few tool calls as possible (max {MAX_TOOL_CALLS}).
A sub-processor with no listed location: follow its url with fetch_page (or search_web) to find where it processes data.
Never guess a country; only commit one you read on a page, with that page as evidence.
Always finish by calling commit exactly once."""

TOOLS = [
    {"type": "function", "function": {
        "name": "recall",
        "description": "Look up this vendor's long-term memory instead of carrying history: "
                       "'subprocessor_history' (when each sub-processor was added/removed), "
                       "'alert_history' (past alerts), 'policy_sentences' (judged training/retention sentences).",
        "parameters": {"type": "object", "properties": {
            "topic": {"type": "string", "enum": ["subprocessor_history", "alert_history", "policy_sentences"]},
            "name": {"type": "string", "description": "optional sub-processor name to filter by"}},
            "required": ["topic"]}}},
    {"type": "function", "function": {
        "name": "search_web", "description": "Web search via Nimble. Returns top results (title, url, snippet).",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "fetch_page", "description": "Fetch a web page via Nimble and return its text (truncated).",
        "parameters": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}}},
    {"type": "function", "function": {
        "name": "judge_sentences",
        "description": "Ask Liquid LFM2.5 whether policy sentences allow training on customer data or state customer-data retention.",
        "parameters": {"type": "object", "properties": {"sentences": {"type": "array", "items": {"type": "string"}}},
                       "required": ["sentences"]}}},
    {"type": "function", "function": {
        "name": "check_fourth_parties",
        "description": "Which watched vendors this vendor depends on, and which of those are red.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "commit",
        "description": "Finish the episode by editing your working memory. Required, exactly once.",
        "parameters": {"type": "object", "properties": {
            "summary": {"type": "string", "description": "one or two sentences: what you checked and concluded"},
            "locations": {"type": "array", "description": "locations you established for sub-processors with no listed country",
                          "items": {"type": "object", "properties": {
                              "subprocessor": {"type": "string"}, "country": {"type": "string"},
                              "evidence_url": {"type": "string"}},
                              "required": ["subprocessor", "country", "evidence_url"]}},
            "note": {"type": "string", "description": "short working note to keep on the card (<= 160 chars)"},
            "check_every": {"type": "integer", "description": "ticks until this vendor needs checking again (1-7)"},
            "memo": {"type": "string", "description": "escalation memo for a compliance officer if there's a violation, else empty"}},
            "required": ["summary", "locations", "note", "memo"]}}},
]


def _nimble():
    key = os.environ.get("NIMBLE_API_KEY", "")
    if not key:
        return None
    from nimble_python import Nimble
    return Nimble(api_key=key)


def _html_to_text(html: str) -> str:
    html = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()


def search_web(query: str) -> str:
    n = _nimble()
    if n is None:
        return "search unavailable (no Nimble key)"
    try:
        r = n.search(query=query, max_results=3, search_depth="lite")
        d = r.model_dump() if hasattr(r, "model_dump") else r
        return json.dumps(d, default=str)[:RESULT_CHARS]
    except Exception as e:
        return f"search failed: {type(e).__name__}: {str(e)[:150]}"


def fetch_page(url: str) -> str:
    n = _nimble()
    local = re.match(r"https?://(localhost|127\.0\.0\.1)", url)
    if n is not None and not local:
        try:
            md = n.extract.run(url=url, formats=["markdown"]).data.markdown or ""
            return f"[via Nimble] {md[:RESULT_CHARS]}"
        except Exception as e:
            fallback = f"Nimble failed ({type(e).__name__}); "
    else:
        fallback = "Nimble unavailable; " if not local else "local URL; "
    try:
        r = requests.get(url, timeout=15)
        return f"[{fallback}fetched directly] {_html_to_text(r.text)[:RESULT_CHARS]}"
    except requests.RequestException as e:
        return f"fetch failed: {e}"


def recall(card: StateCard, store, topic: str, name: str = "") -> str:
    """Long-term memory on demand: compact ledger/verdicts from the card, alerts from the store."""
    key = policy.sub_key(Subprocessor(name=name)) if name else ""
    if topic == "subprocessor_history":
        rows = {k: v for k, v in card.ledger.items() if not key or key in k}
        return json.dumps(rows)[:RESULT_CHARS] or "{}"
    if topic == "policy_sentences":
        return json.dumps([{k: v.get(k) for k in ("kind", "text", "allows", "days", "by")}
                           for v in card.sentence_verdicts.values()])[:RESULT_CHARS]
    alerts = []
    if hasattr(store, "alerts_for"):
        alerts = store.alerts_for(card.vendor)
    elif hasattr(store, "alerts"):
        alerts = [a for a in store.alerts if a.vendor == card.vendor]
    return json.dumps([{"tick": a.tick, "kind": a.kind, "rule": a.rule, "title": a.title} for a in alerts][-10:]) or "[]"


def judge_sentences(sentences: list[str]) -> str:
    from .core import _judge
    verdicts, _, source = _judge([("training", s) for s in sentences[:6]] + [("retention", s) for s in sentences[:6]])
    return json.dumps({"judged_by": source, "verdicts": [
        {"text": v["text"][:120], "kind": v["kind"], "allows_training": v.get("allows"), "retention_days": v.get("days")}
        for v in verdicts.values()]})


def open_questions(card: StateCard) -> list[dict]:
    """Things the rules can't decide on their own: sub-processors with no listed location."""
    return [{"subprocessor": s.name, "url": s.url, "purpose": s.purpose}
            for s in card.subprocessors
            if not s.country.strip() and policy.sub_key(s) not in card.investigations]


def needs_episode(old: StateCard | None, new: StateCard, material: bool) -> str:
    if open_questions(new):
        return "unknown sub-processor location"
    if old is not None and material:
        return "material change"
    return ""


def run_episode(old: StateCard | None, new: StateCard, snap: Snapshot, tick: int, run_id: str,
                trigger: str, changes: list[str], store=None) -> Episode:
    """One fresh-context episode. Mutates `new` only through the commit."""
    t0 = time.monotonic()
    ep = Episode(run_id=run_id, tick=tick, vendor=new.vendor, trigger=trigger, model=llm.REVIEW_MODEL)
    view = new.prompt_view()
    view.pop("clause_quotes", None)
    user = (f"STATE CARD:\n{json.dumps(view, ensure_ascii=False)}\n\n"
            f"CHANGES THIS TICK:\n" + ("\n".join(f"- {c}" for c in changes) or "- none") +
            f"\n\nOPEN QUESTIONS:\n{json.dumps(open_questions(new)) if open_questions(new) else 'none'}")
    messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]
    committed = None
    for _ in range(MAX_TOOL_CALLS + 1):
        try:
            r = llm._reviewer.chat.completions.create(
                model=llm.REVIEW_MODEL, messages=messages, tools=TOOLS, max_tokens=3000,
                extra_body={"reasoning": {"effort": "low"}})
        except Exception as e:
            ep.outcome = f"agent unavailable ({type(e).__name__}); rules-only verdict stands"
            break
        if r.usage:
            ep.input_tokens += r.usage.prompt_tokens
            ep.output_tokens += r.usage.completion_tokens
        msg = r.choices[0].message
        messages.append(msg.model_dump(exclude_none=True))
        if not msg.tool_calls:
            ep.outcome = (msg.content or "").strip()[:400]
            break
        for call in msg.tool_calls:
            name = call.function.name
            try:
                args = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            if name == "commit":
                committed = args
                result = "committed"
            elif len(ep.steps) >= MAX_TOOL_CALLS:
                result = "tool budget exhausted: call commit now"
            elif name == "recall":
                result = recall(new, store, args.get("topic", ""), args.get("name", ""))
            elif name == "search_web":
                result = search_web(args.get("query", ""))
            elif name == "fetch_page":
                result = fetch_page(args.get("url", ""))
            elif name == "judge_sentences":
                result = judge_sentences(args.get("sentences", []))
            elif name == "check_fourth_parties":
                result = json.dumps({"depends_on": new.depends_on, "red_upstream": new.exposed_via})
            else:
                result = f"unknown tool {name}"
            ep.steps.append({"tool": name, "args": args, "result": result[:600]})
            messages.append({"role": "tool", "tool_call_id": call.id, "content": result})
        if committed is not None:
            break
    if committed:
        _apply_commit(new, committed, tick, ep)
    ep.latency_ms = int((time.monotonic() - t0) * 1000)
    return ep


def _apply_commit(card: StateCard, c: dict, tick: int, ep: Episode) -> None:
    """Guardrailed memory edit: only fills unknown locations, never overrides a listed country."""
    unknown = {policy.sub_key(s): s for s in card.subprocessors if not s.country.strip()}
    applied = []
    for loc in c.get("locations") or []:
        k = policy.sub_key(Subprocessor(name=loc.get("subprocessor", "")))
        if k in unknown and loc.get("country") and loc.get("evidence_url"):
            card.investigations[k] = {"country": loc["country"], "evidence_url": loc["evidence_url"],
                                      "tick": tick, "how": "agent episode"}
            applied.append(f"{unknown[k].name}: {loc['country']}")
    for k in unknown:  # asked and still unknown: remember we looked, so we don't loop on it
        card.investigations.setdefault(k, {"country": "", "evidence_url": "", "tick": tick, "how": "not found"})
    if c.get("check_every"):
        card.check_every = 1 if card.is_demo_mirror else max(1, min(7, int(c["check_every"])))
    if c.get("note"):
        card.notes = (f"t{tick} agent: {c['note'][:160]}\n" + card.notes)[:700]
    ep.outcome = (c.get("summary") or "")[:400]
    ep.committed = {"locations": applied, "note": c.get("note", ""), "check_every": c.get("check_every"),
                    "memo": c.get("memo", "")}
