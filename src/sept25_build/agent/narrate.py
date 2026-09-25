"""Live narration for demos: plain-ASCII, timestamped, flushed lines.

Off by default so fixtures/sim/chaos output stays byte-identical. Turn on with
`--narrate` (runner, scripts/tick.py, `python -m sept25_build.agent --fixtures`)
or `narrate.enabled = True`. No ANSI escapes: projector- and log-safe.
"""

from __future__ import annotations

import time

enabled: bool = False


def _ascii(msg: str) -> str:
    return msg.encode("ascii", "replace").decode("ascii")


def say(msg: str) -> None:
    """Print 'HH:MM:SS  msg' when narration is on; no-op otherwise. Never raises."""
    if not enabled:
        return
    try:
        print(f"{time.strftime('%H:%M:%S')}  {_ascii(str(msg))}", flush=True)
    except Exception:  # noqa: BLE001 - narration must never break a tick
        pass


def n(count: int, noun: str) -> str:
    """'1 new sentence', '3 new sentences'."""
    return f"{count} {noun}{'' if count == 1 else 's'}"


def clock(dt) -> str:
    """A fetched_at for humans: '2026-09-25 21:14:03Z'."""
    try:
        return dt.strftime("%Y-%m-%d %H:%M:%S") + ("Z" if dt.utcoffset() is not None and not dt.utcoffset() else "")
    except Exception:  # noqa: BLE001
        return str(dt)


def _short(s: str, n: int = 60) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 3] + "..."


def op_text(o) -> str:
    """One MemoryOp as a short token: 'SET status green->red', 'OPEN_FINDING R1'."""
    kind = o.op.upper()
    if o.op == "set":
        return f"{kind} {o.field} {_short(o.before, 30)}->{_short(o.after, 30)}"
    if o.op in ("open_finding", "close_finding"):
        return f"{kind} {o.field}"
    if o.op == "compact":
        return f"{kind} {o.field} {o.before}->{o.after}"
    if o.op == "keep_on_fetch_gap":
        return f"{kind} (empty fetch, old memory kept)"
    val = o.after if o.op == "remember" else o.before
    return f"{kind} {o.field} " + (f'"{_short(val, 50)}"' if o.field == "sentence" else _short(val))


def ops_line(ops, limit: int = 6) -> str:
    """memory_ops(card, new) as one line, capped so a baseline doesn't flood the screen."""
    parts = [op_text(o) for o in ops[:limit]]
    if len(ops) > limit:
        parts.append(f"+{len(ops) - limit} more")
    return " | ".join(parts) if parts else "(no memory ops)"
