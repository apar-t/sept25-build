"""Run the agent.

    uv run python -m sept25_build.agent              # one live tick against RawTree
    uv run python -m sept25_build.agent --fixtures   # replay fixtures/snapshots in memory (regex, no LLM calls)
    uv run python -m sept25_build.agent --fixtures --llm     # same with Liquid (~10 OpenRouter requests)
    uv run python -m sept25_build.agent --fixtures --write   # same, but also write results to RawTree
"""

import argparse
from pathlib import Path

from ..contracts import Snapshot
from . import MemoryStore, RawTreeStore, llm, run_tick

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "snapshots"


def replay(write: bool, use_llm: bool) -> None:
    if not use_llm:
        llm.disable()  # OpenRouter free tier is ~50 requests/day; opt in with --llm
    store = MemoryStore()
    files = sorted(FIXTURES.glob("*.json"))
    baselines = [f for f in files if f.name.startswith("baseline_")]
    mirror = [f for f in files if f.name.startswith("mirror_")]
    print(f"LLM: {llm.describe()} (regex fallback if unavailable)\n")
    for f in [None] + mirror[1:]:
        batch = baselines + [mirror[0]] if f is None else [f]
        store.snapshots += [Snapshot.model_validate_json(p.read_text()) for p in batch]
        alerts = run_tick(store, run_id="fixtures")
        t = store.ticks[-2 * len(batch):]
        label = "baselines" if f is None else f.stem
        print(f"tick {max(c.tick for c in store.cards.values())} [{label}]")
        for log in t:
            if log.agent == "nights_watch":
                print(f"   {log.vendor:16} material={log.material!s:5} tokens={log.input_tokens:>5}")
        naive = sum(x.input_tokens for x in t if x.agent == "naive")
        print(f"   naive agent context this tick: {naive} tokens")
        for a in alerts:
            print(f"   {'🔴' if a.kind == 'violation' else '🟢'} {a.rule} {a.title}\n      {a.explanation}")
    print("\nfinal cards:")
    for c in store.cards.values():
        print(f"  {c.display_name:24} {c.status:5} findings={[f.rule for f in c.open_findings]} "
              f"training={c.training_on_customer_data} retention={c.retention_days}")
    mirror_card = store.cards.get("tinybird-mirror")
    if mirror_card:
        print(f"\nmirror notes:\n{mirror_card.notes}")
    if write:
        RawTreeStore().write(list(store.cards.values()), store.alerts, store.ticks)
        print("\nwritten to RawTree")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--fixtures", action="store_true")
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--llm", action="store_true", help="use the real LLM in --fixtures (costs requests)")
    args = ap.parse_args()
    if args.fixtures:
        replay(args.write, args.llm)
    else:
        for a in run_tick():
            print(f"{a.kind} {a.rule} {a.title}")
