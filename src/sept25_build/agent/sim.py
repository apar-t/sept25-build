"""Long-horizon simulator: months of vendor pages in seconds. The demo's time-lapse.

    uv run python -m sept25_build.agent.sim --days 90             # regex path, no LLM requests
    uv run python -m sept25_build.agent.sim --days 90 --seed 7    # different noise schedule
    uv run python -m sept25_build.agent.sim --days 90 --llm       # real Liquid calls (OpenRouter ~20 req/min: slow)
    uv run python -m sept25_build.agent.sim --days 90 --write     # also write the TickLogs (run_id sim-90d) to RawTree

Every vendor gets a snapshot every simulated day: usually identical, every 7-12 days some noise
("Last updated" date, rarely whitespace or heading case), plus planted material changes with a
ground truth. Shows the working context staying flat while a naive agent's history keeps growing.
"""

import argparse
import json
import random
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .. import rawtree
from ..contracts import Snapshot
from . import MemoryStore, llm, run_tick

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "snapshots"
T0 = datetime(2026, 6, 1, 12, tzinfo=timezone.utc)  # matches the fixtures' "Last updated: 2026-06-01"
LIQUID_WINDOW = 65_536  # Liquid LFM2.5 context window on OpenRouter
TRAIN_NO = "We do not use customer data to train machine learning or AI models."
TRAIN_YES = ("We may use customer data, including content you submit, to develop and improve our products, "
             "including training our machine learning models.")  # wording from mirror_04
RET_30, RET_60 = "deleted within 30 days of account", "deleted within 60 days of account"
ANALYTICS = {"name": "Example Analytics GmbH", "purpose": "Product analytics", "country": "Germany"}
DATAHARVEST = {"name": "DataHarvest Ltd", "purpose": "Data enrichment and analytics", "country": "Singapore"}

# (day on a 90-day timeline, vendor, change, expected alert (rule, kind) or None if compliant)
EVENTS = [
    (12, "aws", "add_analytics", None),
    (20, "tinybird-mirror", "add_dataharvest", ("R1", "violation")),
    (27, "tinybird-mirror", "remove_dataharvest", ("R1", "resolved")),
    (45, "tinybird-mirror", "add_dataharvest", ("R1", "violation")),
    (60, "nimble", "retention_60", None),
    (75, "liquid", "training_yes", ("R2", "violation")),
    (82, "liquid", "training_no", ("R2", "resolved")),
]


def load_baselines() -> dict[str, dict]:
    files = sorted(FIXTURES.glob("baseline_*.json")) + [FIXTURES / "mirror_01_baseline.json"]
    snaps = [Snapshot.model_validate_json(f.read_text()) for f in files]
    return {s.vendor: s.model_dump(mode="json", exclude={"fetched_at", "snapshot_id"}) for s in snaps}


def apply(s: dict, change: str) -> None:
    subs, text = s["subprocessors"], s["policy_text"]
    if change == "add_analytics":
        subs.append(dict(ANALYTICS))
    elif change == "add_dataharvest":
        subs.append(dict(DATAHARVEST))
    elif change == "remove_dataharvest":
        s["subprocessors"] = [x for x in subs if x["name"] != DATAHARVEST["name"]]
    elif change == "retention_60":
        s["policy_text"] = text.replace(RET_30, RET_60)
    elif change == "training_yes":
        s["policy_text"] = text.replace(TRAIN_NO, TRAIN_YES)
    elif change == "training_no":
        s["policy_text"] = text.replace(TRAIN_YES, TRAIN_NO)


def noise(s: dict, date: datetime, rng: random.Random) -> str:
    """Cosmetic change only. Returns what kind."""
    t, r = s["policy_text"], rng.random()
    if r < 0.1:
        s["policy_text"] = t.replace(".  ", ". ") if ".  " in t else t.replace(". ", ".  ")
        return "whitespace"
    if r < 0.2:
        a, b = ("## Retention", "## RETENTION") if "## Retention" in t else ("## RETENTION", "## Retention")
        s["policy_text"] = t.replace(a, b)
        return "heading case"
    s["policy_text"] = re.sub(r"Last updated: \S+", f"Last updated: {date:%Y-%m-%d}", t)
    return "date"


def simulate(days: int, seed: int) -> tuple[MemoryStore, list[dict], list[tuple], dict]:
    rng = random.Random(seed)
    at = lambda d: min(days - 1, max(1, round(d * days / 90)))  # scale event days to --days
    state = load_baselines()
    events: dict[tuple, list[str]] = {}
    for d, v, change, _ in EVENTS:
        events.setdefault((at(d), v), []).append(change)
    truth = [(v, at(d), *exp) for d, v, _, exp in EVENTS if exp]
    next_noise = {v: rng.randint(7, 12) for v in state}
    store, run_id, days_out, extra = MemoryStore(), f"sim-{days}d", [], {"noise_kinds": {}, "anomalies": []}
    for day in range(days):
        date = T0 + timedelta(days=day)
        kinds = {}
        for v, s in state.items():
            before = json.dumps([s["policy_text"], s["subprocessors"]])
            for change in events.get((day, v), []):
                apply(s, change)
            if day == next_noise[v]:
                k = noise(s, date, rng)
                extra["noise_kinds"][k] = extra["noise_kinds"].get(k, 0) + 1
                next_noise[v] += rng.randint(7, 12)
            changed = json.dumps([s["policy_text"], s["subprocessors"]]) != before
            kinds[v] = "event" if (day, v) in events else "noise" if changed else "same"
            store.snapshots.append(Snapshot(**s, fetched_at=date))
        n0 = len(store.ticks)
        alerts = run_tick(store, run_id=run_id)
        logs = store.ticks[n0:]
        nw = [t for t in logs if t.agent == "nights_watch"]
        cards = [getattr(t, "card_tokens", 0) or llm.count_tokens(json.dumps(store.cards[t.vendor].prompt_view()))
                 for t in nw]
        for t in nw:
            k = kinds.get(t.vendor, "same")
            if day and k == "event" and not t.material:
                extra["anomalies"].append(f"day {day} {t.vendor}: planted change NOT marked material")
            if day and k != "event" and t.material:
                extra["anomalies"].append(f"day {day} {t.vendor}: {k} marked material")
        days_out.append({
            "day": day, "date": date, "card_sum": sum(cards), "card_avg": sum(cards) / max(1, len(cards)),
            "card_max": max(cards, default=0), "llm_calls": sum(t.llm_calls for t in nw),
            "prompt_tokens": sum(t.input_tokens for t in nw), "vendors": len(nw),
            "naive": sum(t.input_tokens for t in logs if t.agent == "naive"),
            "material": sum(t.material for t in nw),
            "noise": sum(kinds.get(t.vendor) == "noise" and not t.material for t in nw),
            "same": sum(kinds.get(t.vendor) == "same" and not t.material for t in nw),
            "alerts": len(alerts)})
    return store, days_out, truth, extra


def spark(vals: list[float], top: float, width: int = 90) -> str:
    bars = "▁▂▃▄▅▆▇█"
    step = max(1, -(-len(vals) // width))
    return "".join(bars[min(7, int(v / top * 8))] if top else bars[0] for v in vals[::step])


def report(store: MemoryStore, rows: list[dict], truth: list[tuple], extra: dict) -> None:
    cum = lambda key, i: sum(r[key] for r in rows[:i + 1])
    print(f"{'day':>4} {'date':>10} {'card tok avg':>12} {'LLM calls':>9} {'LLM prompt tok':>14} "
          f"{'naive ctx':>10} {'noise disc.':>11} {'alerts':>6}")
    for i, r in enumerate(rows):
        if r["day"] % 10 == 0 or i == len(rows) - 1:
            print(f"{r['day']:>4} {r['date']:%Y-%m-%d} {r['card_avg']:>12.0f} {cum('llm_calls', i):>9} "
                  f"{cum('prompt_tokens', i):>14} {r['naive']:>10,} {cum('noise', i):>11} {cum('alerts', i):>6}")

    nw, naive = [r["card_sum"] for r in rows], [r["naive"] for r in rows]
    top = max(naive + nw)
    print(f"\nworking context over {len(rows)} days (same scale, max {top:,} tokens)")
    print(f"  Night's Watch (all cards) {spark(nw, top)}  {nw[0]:,} -> {nw[-1]:,}")
    print(f"  naive (full history)      {spark(naive, top)}  {naive[0]:,} -> {naive[-1]:,}")
    print(f"  Night's Watch, own scale  {spark(nw, max(nw))}  (bounded: notes and counters are compacted)")

    print("\nalerts vs ground truth")
    got = {(a.vendor, a.tick - 1, a.rule, a.kind): a for a in store.alerts}
    matched = [t for t in truth if t in got]
    missed = [t for t in truth if t not in got]
    unexpected = [k for k in got if k not in truth]
    for (v, d, rule, kind), a in sorted(got.items(), key=lambda kv: kv[0][1]):
        tag = "ok" if (v, d, rule, kind) in truth else "UNEXPECTED"
        recur = " [marks recurrence]" if re.search(r"re-added|again|\btime\b", f"{a.title} {a.explanation}", re.I) else ""
        print(f"  day {d:>3} {'🔴' if kind == 'violation' else '🟢'} {rule} {a.title}  ({tag}){recur}")
    for v, d, rule, kind in missed:
        print(f"  day {d:>3} MISSED {rule} {kind} on {v}")
    print(f"  matched {len(matched)}/{len(truth)}, missed {len(missed)}, unexpected {len(unexpected)}")
    for a in extra["anomalies"]:
        print(f"  ! {a}")

    vd = sum(r["vendors"] for r in rows)
    disc = sum(r["noise"] + r["same"] for r in rows)
    over = next((r["day"] for r in rows if r["naive"] > LIQUID_WINDOW), None)
    print(f"\ntotals: {vd} vendor-days observed, {sum(r['llm_calls'] for r in rows)} LLM calls "
          f"({sum(r['prompt_tokens'] for r in rows):,} prompt tokens), {sum(r['material'] for r in rows)} material")
    print(f"  not material (no prompt, no alert): {disc}/{vd} ({100 * disc / max(1, vd):.0f}%): "
          f"{sum(r['noise'] for r in rows)} noise ({', '.join(f'{v} {k}' for k, v in extra['noise_kinds'].items())}), "
          f"{sum(r['same'] for r in rows)} unchanged")
    print(f"  card size: {min(r['card_avg'] for r in rows):.0f}-{max(r['card_max'] for r in rows)} tokens per vendor, "
          f"all cards {nw[-1]:,} vs naive {naive[-1]:,} on the last day ({naive[-1] / max(1, nw[-1]):.0f}x)")
    print(f"  naive context passes Liquid's {LIQUID_WINDOW:,}-token window on "
          + (f"day {over}" if over is not None else f"no day in this run (would need more than {len(rows)} days)"))


def write_ticks(store: MemoryStore) -> None:
    rows = [t.to_row() for t in store.ticks]
    for i in range(0, len(rows), 500):
        rawtree.insert("ticks", rows[i:i + 500])
    print(f"\nwrote {len(rows)} tick rows (run_id {store.ticks[0].run_id}) to RawTree")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--write", action="store_true", help="write the TickLogs (only) to RawTree")
    ap.add_argument("--llm", action="store_true", help="use the real LLM (slow, costs requests)")
    args = ap.parse_args()
    if not args.llm:
        llm.disable()
    print(f"simulating {args.days} days x 6 vendors, seed {args.seed}, LLM: {llm.describe()}\n")
    store, rows, truth, extra = simulate(args.days, args.seed)
    report(store, rows, truth, extra)
    if args.write:
        write_ticks(store)
