"""Audit the source mix across a few recent daily reports.

Counts cited sources (numbered_sources) per section so we can see if SI
is over-represented relative to other outlets.
"""

import io
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def normalize(src: str) -> str:
    s = (src or "").strip()
    if not s:
        return "(unknown)"
    if s.startswith("SI team"):
        return "SI (team page)"
    if s.startswith("SBN "):
        return "SBN (team blog)"
    if s.startswith("BW "):
        return "Beat Writer"
    return s


def audit(path: Path) -> None:
    data = json.loads(io.open(path, encoding="utf-8").read())
    sections = data.get("sections", {}) or {}

    print(f"\n=== {path.name} ===")

    # League-wide + Team Notes share the numbered_sources structure.
    league_sources = (sections.get("league_wide", {}) or {}).get(
        "numbered_sources", []
    )
    if league_sources:
        c = Counter(normalize(s.get("source", "")) for s in league_sources)
        print(f"\n  League-wide: {sum(c.values())} cited sources")
        for src, n in c.most_common():
            print(f"    {n:3d}  {src}")

    # Per-team
    teams = sections.get("team_notes", {}) or {}
    per_team_sources = Counter()
    teams_with_any = 0
    si_dominant_teams = []
    for team, payload in teams.items():
        srcs = (payload or {}).get("numbered_sources", [])
        if not srcs:
            continue
        teams_with_any += 1
        team_counter = Counter(normalize(s.get("source", "")) for s in srcs)
        for k, v in team_counter.items():
            per_team_sources[k] += v
        # Flag teams where SI is the majority of cited sources
        total = sum(team_counter.values())
        si = team_counter.get("SI (team page)", 0)
        if total >= 2 and si >= (total + 1) // 2:
            si_dominant_teams.append((team, si, total))

    if per_team_sources:
        print(f"\n  Team Notes (across {teams_with_any} teams): "
              f"{sum(per_team_sources.values())} cited sources")
        for src, n in per_team_sources.most_common():
            pct = 100 * n / sum(per_team_sources.values())
            print(f"    {n:3d}  ({pct:5.1f}%)  {src}")

    if si_dominant_teams:
        print(f"\n  Teams where SI is >=50% of cited sources "
              f"({len(si_dominant_teams)} teams):")
        for team, si, total in si_dominant_teams:
            print(f"    {team}: {si}/{total}")


def main():
    targets = sys.argv[1:] or sorted(
        (ROOT / "data" / "reports").glob("*.json"),
        reverse=True,
    )[:3]
    for t in targets:
        audit(Path(t))


if __name__ == "__main__":
    main()
