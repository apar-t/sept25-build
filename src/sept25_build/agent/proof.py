"""Proof scorecard: one offline run that backs every number on the pitch slide.

    uv run python -m sept25_build.agent.proof        # < 20 s, no LLM / tokenizer / RawTree requests

1. Long horizon: sim.simulate(365 days, seed 7). Card tokens vs the naive full-history context,
   alerts vs ground truth, share of vendor-days discarded, memory ops vs tick logs.
2. Ledger ablation: the same simulator on 90 days with an agent whose sub-processor ledger is wiped
   before every tick. The day-45 re-add of DataHarvest is then reported as a plain "added".
3. FileStore crash test: chaos.py's 60-day, 8-crash run (seed 3); final memory vs a run that never crashed.

Writes out/proof.json (out/ is gitignored) for the pitch slide and lane C.
"""

import contextlib
import io
import json
import random
import shutil
import sys
import tempfile
from pathlib import Path

from . import chaos, llm, sim
from .store import MemoryStore

OUT = Path(__file__).resolve().parents[3] / "out" / "proof.json"
TOKENS = "estimated as len(text)//4 (no tokenizer requests)"
DAYS, SEED = 365, 7
ABL_DAYS, ABL_EVENT_DAY = 90, 45  # sim.EVENTS day of the second DataHarvest add, on a 90-day timeline
CHAOS_DAYS, CHAOS_CRASHES, CHAOS_SEED = 60, 8, 3
WINDOW = getattr(sim, "LIQUID_WINDOW", 65_536)


def tok(obj) -> int:
    return llm.count_tokens(json.dumps(obj, ensure_ascii=False))


def vs_truth(store, truth: list[tuple]) -> dict:
    """Same matching as sim.report: (vendor, day, rule, kind), with day = alert tick - 1."""
    got = {(a.vendor, a.tick - 1, a.rule, a.kind): a for a in store.alerts}
    return {"matched": sum(t in got for t in truth), "truth": len(truth),
            "missed": sum(t not in got for t in truth), "unexpected": sum(k not in truth for k in got)}


def long_horizon() -> tuple[dict, MemoryStore]:
    store, rows, truth, extra = sim.simulate(DAYS, SEED)
    cards, naive = [r["card_sum"] for r in rows], [r["naive"] for r in rows]
    vd = sum(r["vendors"] for r in rows)
    disc = sum(r["noise"] + r["same"] for r in rows)
    return {
        "days": DAYS, "seed": SEED, "vendors": rows[0]["vendors"],
        "cards_tokens_start": cards[0], "cards_tokens_end": cards[-1], "cards_tokens_max": max(cards),
        "naive_tokens_start": naive[0], "naive_tokens_end": naive[-1],
        "naive_note": "computed with the same tokenizer, not run",
        "ratio_end": round(naive[-1] / max(1, cards[-1]), 1),
        "window_tokens": WINDOW,
        "naive_over_window_day": next((r["day"] for r in rows if r["naive"] > WINDOW), None),
        **vs_truth(store, truth),
        "anomalies": extra["anomalies"],
        "vendor_days": vd, "discarded": disc, "discarded_share": round(disc / max(1, vd), 4),
        "noise": sum(r["noise"] for r in rows), "unchanged": sum(r["same"] for r in rows),
        "llm_calls": sum(r["llm_calls"] for r in rows),
        "memory_ops": len(store.ops), "tick_logs": len(store.ticks),
        "tick_logs_nights_watch": sum(t.agent == "nights_watch" for t in store.ticks),
    }, store


class AmnesicStore(MemoryStore):
    """Ablation: the agent gets its cards back without the sub-processor ledger (long-term memory)."""

    def latest_cards(self):
        cards = super().latest_cards()
        for c in cards.values():
            c.ledger = {}
        return cards


def readd_alert(store, days: int) -> dict | None:
    """The alert on the day of the second DataHarvest add (EVENTS day 45, scaled to `days`)."""
    day = min(days - 1, max(1, round(ABL_EVENT_DAY * days / 90)))
    for a in store.alerts:
        if a.vendor == "tinybird-mirror" and a.tick - 1 == day and a.rule == "R1" and a.kind == "violation":
            return {"day": day, "title": a.title, "explanation": a.explanation}
    return None


def ablation() -> dict:
    with_ledger, _, _, _ = sim.simulate(ABL_DAYS, SEED)
    orig = sim.MemoryStore
    sim.MemoryStore = AmnesicStore
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):  # run_tick prints (and swallows) per-vendor errors
            amnesic, _, truth, _ = sim.simulate(ABL_DAYS, SEED)
    finally:
        sim.MemoryStore = orig
    skipped = [line for line in buf.getvalue().splitlines() if "agent: skipped" in line]
    ledger = with_ledger.cards["tinybird-mirror"].ledger
    return {
        "days": ABL_DAYS, "seed": SEED,
        "with_ledger": readd_alert(with_ledger, ABL_DAYS),
        "without_ledger": readd_alert(amnesic, ABL_DAYS),
        "without_ledger_vs_truth": vs_truth(amnesic, truth),
        "without_ledger_skipped_vendor_ticks": len(skipped), "without_ledger_first_error": skipped[0] if skipped else "",
        "ledger_tokens_tinybird_mirror": tok(ledger),
        "ledger_entries_tinybird_mirror": len(ledger),
        "ledger_tokens_all_vendors": sum(tok(c.ledger) for c in with_ledger.cards.values()),
    }


def crash_test() -> dict:
    """chaos.main's run and comparison, returned as numbers instead of printed."""
    rng = random.Random(CHAOS_SEED)
    days = chaos.timeline(CHAOS_DAYS, CHAOS_SEED)
    tmp = Path(tempfile.mkdtemp(prefix="nw-proof-"))
    try:
        ref_dir, chaos_dir = tmp / "reference", tmp / "chaos"
        chaos.run(ref_dir, days, {}, rng)
        ref = chaos.dump(ref_dir)
        alert_days = sorted({a["tick"] - 1 for a in ref["alerts"].values() if a["tick"] > 1})
        plan = chaos.pick_plan(CHAOS_DAYS, CHAOS_CRASHES, alert_days, rng)
        crashes = chaos.run(chaos_dir, days, plan, rng)
        got = chaos.dump(chaos_dir)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    recovered = sum(c["resumed"] == c["tick"] for c in crashes)
    tables = {}
    for table in ("alerts", "ticks", "memory_ops"):
        r, g = ref[table], got[table]
        tables[table] = {"reference": len(r), "chaos": len(g), "missing": len(set(r) - set(g)),
                         "extra": len(set(g) - set(r)), "dup_ids": len(got[table + "_dups"]),
                         "changed": sum(r[k] != g[k] for k in set(r) & set(g))}
    card_diffs = chaos.diff(ref["cards"], got["cards"], "card")
    return {
        "scope": "FileStore crash test", "days": CHAOS_DAYS, "seed": CHAOS_SEED,
        "crashes": len(crashes), "recoveries": recovered,
        "crash_points": [c["point"] for c in crashes],
        "alert_day_crashes": sum(c["day"] in alert_days for c in crashes),
        "missing_ids": sum(t["missing"] for t in tables.values()),
        "extra_ids": sum(t["extra"] for t in tables.values()),
        "dup_ids": sum(t["dup_ids"] for t in tables.values()),
        "changed_rows": sum(t["changed"] for t in tables.values()),
        "tables": tables, "torn_lines_skipped": got["torn"],
        "cards_identical": not card_diffs, "card_diffs": card_diffs[:5],
    }


def main() -> int:
    llm.disable()                                   # regex path: no LLM requests
    llm.count_tokens = lambda text: len(text) // 4  # token counts are estimates: no tokenizer requests
    print(f"proof: offline, LLM off (regex path), tokens {TOKENS}\n")

    lh, _ = long_horizon()
    print(f"[1] {DAYS}-day simulation x {lh['vendors']} vendors, seed {SEED}")
    print(f"    all cards {lh['cards_tokens_start']:,} -> {lh['cards_tokens_end']:,} tokens (max {lh['cards_tokens_max']:,})")
    print(f"    naive full history {lh['naive_tokens_start']:,} -> {lh['naive_tokens_end']:,} tokens "
          f"({lh['naive_note']})")
    for a in lh["anomalies"]:
        print(f"    ! {a}")

    ab = ablation()
    w, wo = ab["with_ledger"], ab["without_ledger"]
    print(f"\n[2] ledger ablation, {ABL_DAYS}-day simulation, day-{ABL_EVENT_DAY} DataHarvest re-add on tinybird-mirror")
    print(f"    with ledger:    {w['title'] if w else 'NO ALERT'}")
    if w and "Escalate" in w["explanation"]:
        print(f"                    ...{w['explanation'][w['explanation'].index('Recurring'):]}")
    print(f"    without ledger: {wo['title'] if wo else 'NO ALERT'}")
    if ab["without_ledger_skipped_vendor_ticks"]:
        print(f"    ! amnesic run skipped {ab['without_ledger_skipped_vendor_ticks']} vendor-ticks, first: "
              f"{ab['without_ledger_first_error']}")
    print(f"    ledger cost: {ab['ledger_tokens_tinybird_mirror']} tokens for {ab['ledger_entries_tinybird_mirror']} "
          f"sub-processors on tinybird-mirror ({ab['ledger_tokens_all_vendors']} across all vendors), "
          f"kept out of the prompt")

    ct = crash_test()
    print(f"\n[3] {ct['scope']}: {ct['days']} days, {ct['crashes']} crashes at {', '.join(sorted(set(ct['crash_points'])))}")
    for t, s in ct["tables"].items():
        print(f"    {t:<11} reference {s['reference']:>4}  chaos {s['chaos']:>4}  missing {s['missing']}  "
              f"extra {s['extra']}  dup ids {s['dup_ids']}  changed {s['changed']}")

    over = lh["naive_over_window_day"]
    recur = bool(w and "re-added" in w["title"] and "Escalate" in w["explanation"])
    plain = bool(wo and " added " in wo["title"] and "re-added" not in wo["title"])
    lines = [
        f"Bounded memory: all {lh['vendors']} cards {lh['cards_tokens_start']:,} -> {lh['cards_tokens_end']:,} tokens over "
        f"{DAYS} days; naive history {lh['naive_tokens_start']:,} -> {lh['naive_tokens_end']:,} "
        f"({lh['naive_note']}), {lh['ratio_end']:.0f}x on the last day",
        f"Naive context passes Liquid's {WINDOW:,}-token window on "
        + (f"day {over}" if over is not None else f"no day in {DAYS}") + "; Night's Watch never grows past "
        f"{lh['cards_tokens_max']:,} for all vendors",
        f"Alerts vs ground truth: matched {lh['matched']}/{lh['truth']}, missed {lh['missed']}, "
        f"unexpected {lh['unexpected']}",
        f"Discarded without a prompt or alert: {lh['discarded']:,}/{lh['vendor_days']:,} vendor-days "
        f"({100 * lh['discarded_share']:.0f}%); audit trail {lh['memory_ops']:,} memory ops for {lh['tick_logs']:,} tick logs "
        f"({lh['tick_logs_nights_watch']:,} agent + {lh['tick_logs'] - lh['tick_logs_nights_watch']:,} naive baseline)",
        f"Ledger ablation: with it, day {ABL_EVENT_DAY} says 're-added ... 2nd time, Escalate' "
        f"({'YES' if recur else 'NO'}); without it, plain 'added' ({'YES' if plain else 'NO'}); "
        f"ledger costs {ab['ledger_tokens_tinybird_mirror']} tokens",
        f"FileStore crash test: {ct['crashes']} crashes, {ct['recoveries']} recoveries, {ct['missing_ids']} missing ids, "
        f"{ct['dup_ids']} dup ids, final cards identical: {'YES' if ct['cards_identical'] else 'NO'}",
    ]
    print("\nscorecard")
    for line in lines:
        print(f"  {line}")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"tokens": TOKENS, "scorecard": lines, "long_horizon": lh, "ledger_ablation": ab,
                               "crash_test": ct}, indent=2, default=str) + "\n")
    print(f"\nwrote {OUT.relative_to(OUT.parents[1])}")
    ok = (lh["missed"] == 0 and lh["unexpected"] == 0 and recur and plain and ct["cards_identical"]
          and not ab["without_ledger_skipped_vendor_ticks"]
          and ct["recoveries"] == ct["crashes"] and ct["missing_ids"] == 0 and ct["dup_ids"] == 0)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
