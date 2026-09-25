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
import sys
import time
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv

from ..contracts import APPROVED_COUNTRIES, COMPANY, RULES, Episode, Snapshot, StateCard, Subprocessor
from . import llm, narrate, policy

load_dotenv()

MAX_TOOL_CALLS = 6
RESULT_CHARS = 2500
EPISODE_BUDGET_S = 40   # wall clock; past it the agent is forced to commit what it has
RETRY_TICKS = 3         # a 'not found' location is re-asked this many ticks later

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
    return Nimble(api_key=key, timeout=20, max_retries=0)  # a stuck fetch falls back instead of stalling the demo


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
            md = n.extract.run(url=url, formats=["markdown"], render=False).data.markdown or ""  # static pages: ~1s
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
    """Things the rules can't decide on their own: sub-processors with no listed location.
    A 'not found' (or rejected) answer is re-asked once card.tick reaches its retry_after."""
    out = []
    for s in card.subprocessors:
        if s.country.strip():
            continue
        inv = card.investigations.get(policy.sub_key(s))
        if inv and (inv.get("country") or card.tick < inv.get("retry_after", inv.get("tick", 0) + RETRY_TICKS)):
            continue
        out.append({"subprocessor": s.name, "url": s.url, "purpose": s.purpose})
    return out


def needs_episode(old: StateCard | None, new: StateCard, material: bool) -> str:
    if open_questions(new):
        return "unknown sub-processor location"
    if old is not None and material:
        return "material change"
    return ""


def _one_line(text: str, n: int = 80) -> str:
    return re.sub(r"\s+", " ", str(text)).strip()[:n]


def _commit_args(msg) -> dict | None:
    for call in msg.tool_calls or []:
        if call.function.name == "commit":
            try:
                return json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                return {}
    return None


def run_episode(old: StateCard | None, new: StateCard, snap: Snapshot, tick: int, run_id: str,
                trigger: str, changes: list[str], store=None) -> Episode:
    """One fresh-context episode. Mutates `new` only through the commit."""
    t0 = time.monotonic()
    ep = Episode(run_id=run_id, tick=tick, vendor=new.vendor, trigger=trigger, model=llm.REVIEW_MODEL)
    view = new.prompt_view()
    view.pop("clause_quotes", None)
    questions = open_questions(new)
    user = (f"STATE CARD:\n{json.dumps(view, ensure_ascii=False)}\n\n"
            f"CHANGES THIS TICK:\n" + ("\n".join(f"- {c}" for c in changes) or "- none") +
            f"\n\nOPEN QUESTIONS:\n{json.dumps(questions) if questions else 'none'}")
    messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]
    narrate.say(f"agent: episode on {new.vendor} ({trigger}); open questions: "
                + (", ".join(q["subprocessor"] for q in questions) or "none"))
    committed, unavailable = None, False
    seen: list[tuple[str, str]] = []   # (url, full result text) of every fetch/search this episode
    for _ in range(MAX_TOOL_CALLS + 1):
        if time.monotonic() - t0 > EPISODE_BUDGET_S:
            narrate.say(f"agent: {EPISODE_BUDGET_S}s budget used, forcing commit")
            break
        try:
            r = llm._reviewer.chat.completions.create(
                model=llm.REVIEW_MODEL, messages=messages, tools=TOOLS, max_tokens=3000,
                extra_body={"reasoning": {"effort": "low"}})
        except Exception as e:
            ep.outcome = f"agent unavailable ({type(e).__name__}); rules-only verdict stands"
            narrate.say(f"agent: {ep.outcome}")
            unavailable = True
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
            t1 = time.monotonic()
            arg = ""
            if name == "commit":
                committed = args
                result = "committed"
            elif len(ep.steps) >= MAX_TOOL_CALLS:
                result = "tool budget exhausted: call commit now"
            elif name == "recall":
                arg = args.get("topic", "")
                result = recall(new, store, arg, args.get("name", ""))
            elif name == "search_web":
                arg = args.get("query", "")
                result = search_web(arg)
                if not result.startswith(("search failed", "search unavailable")):
                    seen.append((f"search: {arg}", result))
            elif name == "fetch_page":
                arg = args.get("url", "")
                result = fetch_page(arg)
                if not result.startswith("fetch failed"):
                    seen.append((arg, result))
            elif name == "judge_sentences":
                arg = f"{len(args.get('sentences', []))} sentences"
                result = judge_sentences(args.get("sentences", []))
            elif name == "check_fourth_parties":
                result = json.dumps({"depends_on": new.depends_on, "red_upstream": new.exposed_via})
            else:
                result = f"unknown tool {name}"
            if name != "commit":
                narrate.say(f"  tool {name}({_one_line(arg, 70)}) -> {_one_line(result)} "
                            f"[{time.monotonic() - t1:.1f}s]")
                ep.steps.append({"tool": name, "args": args, "result": result[:600]})
            messages.append({"role": "tool", "tool_call_id": call.id, "content": result})
        if committed is not None:
            break
    if committed is None and not unavailable:
        # budget spent or the model stopped without committing: one last call that must be `commit`
        if messages[-1].get("role") == "assistant" and not messages[-1].get("tool_calls"):
            messages.append({"role": "user", "content": "Call commit now. Only include locations you read on a page "
                                                        "fetched in this episode, with that page as evidence_url."})
        narrate.say("agent: no commit yet, forcing one final commit call")
        try:
            r = llm._reviewer.chat.completions.create(
                model=llm.REVIEW_MODEL, messages=messages, tools=TOOLS, max_tokens=3000,
                tool_choice={"type": "function", "function": {"name": "commit"}},
                extra_body={"reasoning": {"effort": "low"}})
            if r.usage:
                ep.input_tokens += r.usage.prompt_tokens
                ep.output_tokens += r.usage.completion_tokens
            committed = _commit_args(r.choices[0].message) if r.choices else None
        except Exception as e:
            narrate.say(f"agent: forced commit failed ({type(e).__name__}); rules-only verdict stands")
    if committed is not None:
        _apply_commit(new, committed, tick, ep, seen)
    ep.latency_ms = int((time.monotonic() - t0) * 1000)
    narrate.say(f"agent: episode done in {ep.latency_ms / 1000:.1f}s, {len(ep.steps)} tool calls, "
                f"{ep.input_tokens + ep.output_tokens} tokens")
    return ep


def _path_key(url: str) -> str:
    """Host-insensitive, trailing-slash-stripped path (host only for a bare root)."""
    u = urlparse((url or "").strip())
    return u.path.rstrip("/").lower() or u.netloc.lower()


def _mentions(text: str, country: str) -> bool:
    return bool(re.search(r"(?<![a-z])" + re.escape(country.lower()) + r"(?![a-z])", text.lower()))


def _ground(country: str, evidence_url: str, seen: list[tuple[str, str]]) -> str:
    """The fetched URL whose text shows `country`, preferring the page the agent cited; '' if none."""
    hits = [(u, t) for u, t in seen if _mentions(t, country)]
    want = _path_key(evidence_url)
    for u, t in hits:
        if not u.startswith("search: ") and want and _path_key(u) == want:
            return u
    for u, t in hits:  # a search result snippet that names both the cited url and the country
        if u.startswith("search: ") and evidence_url and evidence_url.rstrip("/") in t:
            return evidence_url
    return hits[0][0] if hits else ""


def _apply_commit(card: StateCard, c: dict, tick: int, ep: Episode, seen: list[tuple[str, str]] | None = None) -> None:
    """Guardrailed memory edit: only fills unknown locations, never overrides a listed country, and only
    accepts a country that appears on a page fetched (or a search run) in this episode."""
    seen = seen or []
    unknown = {policy.sub_key(s): s for s in card.subprocessors if not s.country.strip()}
    applied, rejected = [], []
    for loc in c.get("locations") or []:
        name, country = str(loc.get("subprocessor", "")), str(loc.get("country", "")).strip()
        k = policy.sub_key(Subprocessor(name=name))
        if k not in unknown or not country:
            if name or country:
                rejected.append({"subprocessor": name, "country": country,
                                 "reason": "not an open question" if k not in unknown else "no country"})
            continue
        known = card.investigations.get(k, {})
        if known.get("country", "").lower() == country.lower():
            continue  # restating a location it already verified earlier: nothing to change, nothing to reject
        where = _ground(country, str(loc.get("evidence_url", "")), seen)
        if not where:
            rejected.append({"subprocessor": name, "country": country,
                             "reason": "country not on any fetched page"})
            narrate.say(f"  commit {unknown[k].name}: {country} REJECTED: country not on any fetched page")
            continue
        card.investigations[k] = {"country": country, "evidence_url": where, "tick": tick, "how": "agent episode"}
        applied.append(f"{unknown[k].name}: {country}")
        shown = where if where.startswith("search: ") else (urlparse(where).path or where)
        narrate.say(f"  commit {unknown[k].name}: {country}, verified on {shown}")
    for k in unknown:  # asked and still unknown: remember we looked, re-ask after RETRY_TICKS
        if not card.investigations.get(k, {}).get("country"):
            card.investigations[k] = {"country": "", "evidence_url": "", "tick": tick, "how": "not found",
                                      "retry_after": tick + RETRY_TICKS}
    if c.get("check_every"):
        try:
            card.check_every = 1 if card.is_demo_mirror else max(1, min(7, int(c["check_every"])))
        except (TypeError, ValueError):
            pass
    if c.get("note"):
        card.notes = (f"t{tick} agent: {c['note'][:160]}\n" + card.notes)[:700]
    ep.outcome = (c.get("summary") or "")[:400]
    ep.committed = {"locations": applied, "rejected": rejected, "note": c.get("note", ""),
                    "check_every": c.get("check_every"), "memo": c.get("memo", "")}


def _selftest() -> int:
    """Offline: the commit guard rejects a fabricated evidence URL and a fetched-but-wrong country. No LLM."""
    tunnel = "https://demo.trycloudflare.com"
    dh = Subprocessor(name="DataHarvest Ltd", purpose="Analytics", country="", url=f"{tunnel}/companies/dataharvest")
    page = (f"{tunnel}/companies/dataharvest",
            "[via Nimble] DataHarvest Ltd. Headquarters: Singapore, Singapore. Data processing locations: Singapore")
    other = (f"{tunnel}/companies/otherco", "[via Nimble] OtherCo. Data processing locations: Germany")

    def run(locs, seen, tick=5):
        card = StateCard(vendor="tinybird-mirror", display_name="Tinybird (demo mirror)", is_demo_mirror=True,
                         subprocessors=[dh], tick=tick)
        ep = Episode(run_id="selftest", tick=tick, vendor=card.vendor, trigger="selftest", model="none")
        _apply_commit(card, {"summary": "t", "locations": locs, "note": "", "memo": ""}, tick, ep, seen)
        return card, ep

    k = policy.sub_key(dh)
    # 1. fabricated evidence URL, nothing on any fetched page says Singapore
    card, ep = run([{"subprocessor": "DataHarvest Ltd", "country": "Singapore",
                     "evidence_url": "https://dataharvest.example/locations"}], [other])
    assert not ep.committed["locations"] and ep.committed["rejected"], ep.committed
    assert ep.committed["rejected"][0]["reason"] == "country not on any fetched page"
    assert card.investigations[k]["country"] == "" and card.investigations[k]["retry_after"] == 8
    # 2. the cited page was fetched, but the committed country is not on it (or on any other fetched page)
    card, ep = run([{"subprocessor": "DataHarvest Ltd", "country": "India",
                     "evidence_url": f"{tunnel}/companies/dataharvest"}], [page, other])
    assert not ep.committed["locations"] and ep.committed["rejected"][0]["country"] == "India", ep.committed
    assert card.investigations[k]["country"] == ""
    # 3. retry: still closed before retry_after, re-asked from retry_after on
    assert open_questions(card) == []
    card.tick = 8
    assert [q["subprocessor"] for q in open_questions(card)] == ["DataHarvest Ltd"]
    # 4. grounded: cited path matches a fetched page on another host, trailing slash ignored
    card, ep = run([{"subprocessor": "DataHarvest Ltd", "country": "Singapore",
                     "evidence_url": "http://127.0.0.1:8765/companies/dataharvest/"}], [other, page])
    assert ep.committed["locations"] == ["DataHarvest Ltd: Singapore"], ep.committed
    assert card.investigations[k]["evidence_url"] == page[0] and "retry_after" not in card.investigations[k]
    # 5. fabricated URL but the country is on a fetched page: that page becomes the evidence
    card, ep = run([{"subprocessor": "DataHarvest Ltd", "country": "Singapore",
                     "evidence_url": "https://made.up/about"}], [other, page])
    assert card.investigations[k]["evidence_url"] == page[0], card.investigations[k]
    # 6. a listed-country sub-processor can't be overridden
    card, ep = run([{"subprocessor": "Amazon Web Services, Inc.", "country": "Singapore",
                     "evidence_url": page[0]}], [page])
    assert not ep.committed["locations"] and ep.committed["rejected"][0]["reason"] == "not an open question"
    print("episode selftest: OK (fabricated url rejected, wrong country rejected, retry_after re-asks, "
          "grounded commits keep the fetched page as evidence)")
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        narrate.enabled = "--narrate" in sys.argv
        sys.exit(_selftest())
    print("usage: python -m sept25_build.agent.episode --selftest [--narrate]")
