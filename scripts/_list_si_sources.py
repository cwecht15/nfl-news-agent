"""List every SI-prefixed source seen in recent reports — sanity check
that the new "SI " prefix doesn't catch unrelated outlets."""

import glob
import io
import json
from collections import Counter

c = Counter()
for p in glob.glob("data/reports/2026-05-*.json"):
    try:
        d = json.loads(io.open(p, encoding="utf-8").read())
    except Exception:
        continue
    for team in d.get("team_highlights", {}).values():
        for s in team.get("numbered_sources", []):
            src = s.get("source", "")
            if src.startswith("SI"):
                c[src] += 1

print("Unique SI-prefixed source labels across May 2026 reports:")
for src, n in c.most_common():
    print(f"  {n:3d}  {src!r}")
