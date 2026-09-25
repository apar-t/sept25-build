"""Supply-chain graph: link sub-processors to vendors we already watch ("link, don't crawl").

Lane A fetches one level per vendor. Deeper structure comes from matching names: if Tinybird's
list contains "Amazon Web Services", the edge points at the AWS node we already watch. Every
company is one node, and traversal keeps a visited set, so cycles can't loop.
"""

import re

from ..contracts import VENDOR_ALIASES, StateCard, Subprocessor

_PATTERNS = {slug: [re.compile(rf"(?<![a-z0-9]){re.escape(a)}(?![a-z0-9])") for a in aliases]
             for slug, aliases in VENDOR_ALIASES.items()}


def vendor_for(sub: Subprocessor) -> str | None:
    """The watched vendor this sub-processor is, if any."""
    name = sub.name.lower()
    for slug, pats in _PATTERNS.items():
        if any(p.search(name) for p in pats):
            return slug
    return None


def link(cards: dict[str, StateCard]) -> list[StateCard]:
    """Set depends_on / exposed_via on every card. Returns the cards whose links changed."""
    base = {v: v.removesuffix("-mirror") for v in cards}          # the mirror stands in for its vendor
    node = {}                                                      # vendor slug -> the card that represents it
    for v in cards:
        node.setdefault(base[v], v)
        if v == base[v]:
            node[base[v]] = v                                      # a real (non-mirror) card wins if both exist
    deps = {}
    for v, c in cards.items():
        targets = {vendor_for(s) for s in c.subprocessors} - {None, base[v]}
        deps[v] = sorted(node[t] for t in targets if t in node)

    def reach(start: str) -> list[str]:
        seen, stack, red = {start}, list(deps[start]), []
        while stack:
            n = stack.pop()
            if n in seen:
                continue                                           # cycle or shared dependency
            seen.add(n)
            if cards[n].status == "red":
                red.append(n)
            stack += deps.get(n, [])
        return sorted(red)

    changed = []
    for v, c in cards.items():
        exposed = reach(v)
        if c.depends_on != deps[v] or c.exposed_via != exposed:
            c.depends_on, c.exposed_via = deps[v], exposed
            changed.append(c)
    return changed
