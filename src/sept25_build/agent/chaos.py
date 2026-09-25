"""Crash-recovery proof: kill the agent anywhere inside a tick, restart it, and it resumes from its
persisted memory with no lost events and no duplicate alerts. The final memory is identical to
a run that never crashed.

    uv run python -m sept25_build.agent.chaos --days 60 --crashes 8 --seed 3

1. Reference run: the simulator's timeline (see sim.py), one run_tick per simulated day, on a
   FileStore that never crashes.
2. Chaos run: same timeline, fresh FileStore. On chosen days, store.write() dies at a chosen point
   (before writing, after ops, after alerts, after tick logs = just before the cards commit, or
   halfway through a card line). On a crash every in-memory object is dropped, a NEW FileStore is
   opened on the same directory (a process restart: memory comes back from disk only) and the
   tick is run again. Half the crashes are aimed at days that raise alerts, and one day is killed
   twice: the restarted agent dies again while it is replaying.
3. Compare: final cards, and every alert / tick log / memory op by its idempotency key.
"""

import argparse
import json
import random
import shutil
import sys
import tempfile
from collections import Counter
from datetime import timedelta
from pathlib import Path

from ..contracts import Snapshot
from . import llm, run_tick
from .filestore import KEYS, FileStore
from .sim import EVENTS, T0, apply, load_baselines, noise

POINTS = ["before_write", "after_ops", "after_alerts", "after_ticks", "mid_line"]
RUN_ID = "chaos"


class SimulatedCrash(BaseException):
    """BaseException, like SIGKILL: no `except Exception` in the agent can swallow it."""


def timeline(days: int, seed: int) -> list[list[Snapshot]]:
    """The snapshots sim.simulate() feeds the agent, day by day (same RNG sequence), without running it."""
    rng = random.Random(seed)
    at = lambda d: min(days - 1, max(1, round(d * days / 90)))  # noqa: E731
    state = load_baselines()
    events: dict[tuple, list[str]] = {}
    for d, v, change, _ in EVENTS:
        events.setdefault((at(d), v), []).append(change)
    next_noise = {v: rng.randint(7, 12) for v in state}
    out = []
    for day in range(days):
        date, snaps = T0 + timedelta(days=day), []
        for v, s in state.items():
            for change in events.get((day, v), []):
                apply(s, change)
            if day == next_noise[v]:
                noise(s, date, rng)
                next_noise[v] += rng.randint(7, 12)
            snaps.append(Snapshot(**s, fetched_at=date))
        out.append(snaps)
    return out


class CrashingStore(FileStore):
    """A FileStore whose write() dies at `point`. Everything it wrote before dying stays on disk."""
    BEFORE = {"alerts": "after_ops", "ticks": "after_alerts", "state_cards": "after_ticks"}

    def __init__(self, root, point: str, rng: random.Random):
        super().__init__(root)
        self.point, self.rng, self.note = point, rng, ""

    def write(self, cards, alerts, ticks, ops=()):
        if self.point == "before_write":
            raise SimulatedCrash
        super().write(cards, alerts, ticks, ops)

    def _append(self, table, rows):
        if self.BEFORE.get(table) == self.point:
            raise SimulatedCrash
        if table == "state_cards" and self.point == "mid_line" and rows:
            lines = self._lines(rows)
            k = self.rng.randrange(len(lines))
            self._append_raw(table, b"".join(lines[:k]) + lines[k][:len(lines[k]) // 2])
            self.note = f"{k} of {len(lines)} card lines written, then half of the next"
            raise SimulatedCrash
        super()._append(table, rows)


def next_tick(store: FileStore) -> int:
    return max((c.tick for c in store.latest_cards().values()), default=0) + 1


def run(root: Path, days: list[list[Snapshot]], plan: dict[int, list[str]], rng: random.Random) -> list[dict]:
    store, crashes = FileStore(root), []
    for day, snaps in enumerate(days):
        store.add_snapshots(snaps)
        for point in plan.get(day, []):
            tick = next_tick(store)
            victim = CrashingStore(root, point, rng)
            try:
                run_tick(victim, run_id=RUN_ID)
            except SimulatedCrash:
                note = victim.note
                del victim, store      # the process is gone: nothing in memory survives
                store = FileStore(root)  # restart: memory is recovered from disk only
                crashes.append({"day": day, "point": point, "tick": tick, "resumed": next_tick(store), "note": note})
                continue
            crashes.append({"day": day, "point": point, "tick": tick, "resumed": None, "note": "crash point not reached"})
            store = FileStore(root)
            break  # the tick completed, nothing left to replay today
        else:
            run_tick(store, run_id=RUN_ID)
    return crashes


def dump(root: Path) -> dict:
    s = FileStore(root)
    out = {"cards": {v: c.model_dump(mode="json", exclude={"updated_at"}) for v, c in s.latest_cards().items()}}
    for table, ms, skip in (("alerts", s.all_alerts(), {"created_at"}), ("ticks", s.all_ticks(), {"created_at", "latency_ms"}),
                            ("memory_ops", s.all_ops(), {"created_at"})):
        key = KEYS[table]
        out[table] = {getattr(m, key): m.model_dump(mode="json", exclude=skip) for m in ms}
        out[table + "_dups"] = {k: n for k, n in Counter(getattr(m, key) for m in ms).items() if n > 1}
    torn = 0
    for f in root.glob("*.jsonl"):
        torn += sum(1 for line in f.read_bytes().splitlines() if line.strip() and not _parses(line))
    out["torn"] = torn
    return out


def _parses(line: bytes) -> bool:
    try:
        json.loads(line)
        return True
    except ValueError:
        return False


def pick_plan(n_days: int, crashes: int, alert_days: list[int], rng: random.Random) -> dict[int, list[str]]:
    points = [POINTS[i % len(POINTS)] for i in range(crashes)]
    rng.shuffle(points)
    distinct = min(n_days, crashes - 1 if crashes >= 3 else crashes)
    hot = rng.sample(alert_days, min(len(alert_days), (distinct + 1) // 2))
    cold = [d for d in range(n_days) if d not in hot]
    chosen = hot + rng.sample(cold, min(len(cold), distinct - len(hot)))
    slots = chosen + ([chosen[0]] if crashes >= 3 and chosen else [])  # one day is killed twice
    plan: dict[int, list[str]] = {}
    for d, p in zip(slots, points):
        plan.setdefault(d, []).append(p)
    return plan


def diff(a, b, path="", out=None, limit=12):
    out = [] if out is None else out
    if len(out) >= limit:
        return out
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b), key=str):
            if k not in a or k not in b:
                out.append(f"{path}.{k}: {'missing in chaos run' if k in a else 'only in chaos run'}")
            else:
                diff(a[k], b[k], f"{path}.{k}", out, limit)
    elif a != b:
        out.append(f"{path}: reference {str(a)[:90]!r} != chaos {str(b)[:90]!r}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--crashes", type=int, default=8)
    ap.add_argument("--seed", type=int, default=3)
    ap.add_argument("--keep", action="store_true", help="keep the two store directories for inspection")
    args = ap.parse_args()
    llm.disable()                               # deterministic regex path, no LLM requests
    llm.count_tokens = lambda text: len(text) // 4  # no tokenizer requests either
    rng = random.Random(args.seed)
    days = timeline(args.days, args.seed)
    tmp = Path(tempfile.mkdtemp(prefix="nw-chaos-"))
    ref_dir, chaos_dir = tmp / "reference", tmp / "chaos"

    print(f"chaos test: {args.days} days x {len(days[0])} vendors, {args.crashes} crashes, seed {args.seed}, "
          f"LLM off (regex path)\n")
    run(ref_dir, days, {}, rng)
    ref = dump(ref_dir)
    alert_days = sorted({a["tick"] - 1 for a in ref["alerts"].values() if a["tick"] > 1})
    print(f"reference run (never crashes): {len(ref['cards'])} cards, {len(ref['alerts'])} alerts, "
          f"{len(ref['ticks'])} tick logs, {len(ref['memory_ops'])} memory ops\n")

    plan = pick_plan(args.days, args.crashes, alert_days, rng)
    crashes = run(chaos_dir, days, plan, rng)
    got = dump(chaos_dir)
    for i, c in enumerate(crashes, 1):
        hot = " (alert day)" if c["day"] in alert_days else ""
        if c["resumed"] is None:
            print(f"  crash {i}: day {c['day']:>2}{hot} {c['point']:<12} NOT TRIGGERED: {c['note']}")
            continue
        ok = "" if c["resumed"] == c["tick"] else f"  !! expected tick {c['tick']}"
        extra = f"; {c['note']}" if c["note"] else ""
        print(f"  crash {i}: day {c['day']:>2}{hot:<12} killed {c['point']:<12} -> restarted from disk, "
              f"recovered: resumed at tick {c['resumed']}{ok}{extra}")

    problems = []
    recovered = sum(c["resumed"] == c["tick"] for c in crashes)
    if recovered != len(crashes):
        problems.append(f"{len(crashes) - recovered} crash(es) did not resume at the crashed tick")
    lost = {}
    print(f"\n{'':<14}{'reference':>10}{'chaos':>8}{'missing':>9}{'extra':>7}{'dup ids':>9}{'changed':>9}")
    for table in ("alerts", "ticks", "memory_ops"):
        r, g = ref[table], got[table]
        missing, extra = sorted(set(r) - set(g)), sorted(set(g) - set(r))
        changed = [k for k in set(r) & set(g) if r[k] != g[k]]
        lost[table] = len(missing)
        print(f"  {table:<12}{len(r):>10}{len(g):>8}{len(missing):>9}{len(extra):>7}{len(got[table + '_dups']):>9}"
              f"{len(changed):>9}")
        problems += [f"{table}: missing {k}" for k in missing[:5]] + [f"{table}: extra {k}" for k in extra[:5]]
        problems += [f"{table}: duplicate id {k} x{n}" for k, n in list(got[table + "_dups"].items())[:5]]
        problems += [f"{table}: {k} {d}" for k in sorted(changed)[:3] for d in diff(r[k], g[k], "")[:4]]
    card_diffs = diff(ref["cards"], got["cards"], "card")
    same = not card_diffs
    print(f"  {'final cards':<12}{len(ref['cards']):>10}{len(got['cards']):>8}   "
          f"{'identical (facts, findings, status, notes, ledger, counters, sentence verdicts)' if same else 'DIFFERENT'}")
    print(f"  torn lines left on disk by the crashes (skipped on read): {got['torn']}")
    problems += card_diffs
    dups = sum(len(got[t + "_dups"]) for t in ("alerts", "ticks", "memory_ops"))

    print(f"\n{len(crashes)} crashes, {recovered} recoveries, {lost['alerts']} lost alerts, "
          f"{lost['ticks'] + lost['memory_ops']} lost tick logs/memory ops, {dups} duplicates, "
          f"final memory identical: {'YES' if same else 'NO'}")
    if args.keep:
        print(f"stores kept in {tmp}")
    else:
        shutil.rmtree(tmp, ignore_errors=True)
    if problems:
        print("\nFAILED, first differences:")
        for p in problems[:20]:
            print(f"  - {p}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
