"""One team's slice of every in-season data file — the Team page's loaders.

Each function takes an already-loaded file (week file, state, audit,
snapshot ...) plus a news-style team abbreviation and returns plain rows, so
the page stays layout and these stay testable. Sources speak different team
dialects; everything is normalized here through ``processing.team_abbr``:
audit alerts and the weekly sheet use projection style (ARZ / BLT / HST),
OurLads its own slugs, everything else news style.
"""

from __future__ import annotations

from typing import Any, Optional

from processing.season import weekday_name
from processing.team_abbr import to_news, to_proj

RESERVE_OR_PS = ("IR", "PUP", "NFI", "SUS", "PS", "EXE")
SKILL = ("QB", "RB", "FB", "WR", "TE", "K")
GAME_STATUS_LABEL = {"OUT": "Out", "D": "Doubtful", "Q": "Questionable"}


def injury_rows(week_file: Optional[dict], team: str) -> tuple[list[str], list[dict]]:
    """(``"Wed 09-16"``-style day labels, one row per listed player).

    Players dropped from the club's report (``cleared``) are left out; the
    days are the team's own report days, so a Sunday club never shows the
    Thursday-night club's Monday column.
    """
    t = ((week_file or {}).get("teams") or {}).get(team) or {}
    players = [p for p in (t.get("players") or {}).values() if not p.get("cleared")]
    days = sorted(t.get("practice_days") or {d for p in players for d in (p.get("practice") or {})})
    labels = {d: f"{weekday_name(d)} {d[5:]}" for d in days}
    rows = []
    for p in sorted(players, key=lambda p: (p.get("game_status") == "", p.get("name", ""))):
        row = {"Player": p.get("name", ""), "Pos": p.get("pos", ""), "Injury": p.get("injury", "")}
        for d in days:
            row[labels[d]] = (p.get("practice") or {}).get(d, "")
        row["Game status"] = GAME_STATUS_LABEL.get(p.get("game_status", ""), p.get("game_status", ""))
        rows.append(row)
    return [labels[d] for d in days], rows


REFRESH_SCHEDULE = ("Refreshed each morning and evening, plus Wed/Thu 5 PM, Fri every 45 min "
                    "3:45–6:45 PM and Sat 4:30 PM ET, when practice reports and designations post.")


def designation_note(week_file: Optional[dict], team: str, today: str) -> str:
    """Why a team's Game status column is empty, when it is.

    Designations come with a club's final practice report (Fri for Sunday,
    Sat for Monday, Wed for Thursday) and West Coast clubs post theirs late
    in the afternoon, so an empty column on that day usually means "not
    posted yet" rather than "nobody designated"."""
    t = ((week_file or {}).get("teams") or {}).get(team) or {}
    days = sorted(t.get("practice_days") or [])
    players = [p for p in (t.get("players") or {}).values() if not p.get("cleared")]
    if not days or not players or any(p.get("game_status") for p in players):
        return ""
    last = days[-1]
    if today < last:
        return f"Game designations come with the {weekday_name(last)} report."
    if today == last:
        return ("No game designations yet — they come with today's final report, which this club "
                "had not posted as of the last refresh.")
    return ""


def inactive_rows(inactives_week: Optional[dict], team: str) -> list[dict]:
    rows = []
    for g in ((inactives_week or {}).get("games") or {}).values():
        t = (g.get("teams") or {}).get(team)
        if not t:
            continue
        for p in t.get("inactives") or []:
            rows.append({"Player": p.get("name", ""), "Pos": p.get("pos", ""),
                         "Game": g.get("short_name", ""), "Phase": t.get("phase", "")})
    rows.sort(key=lambda r: (r["Pos"] not in SKILL, r["Pos"], r["Player"]))
    return rows


def roster_rows(state: Optional[dict], team: str, skill_only: bool = False) -> list[dict]:
    """Players off the active 53 (reserve lists, practice squad), with return
    eligibility and elevations used."""
    rows = []
    for p in ((state or {}).get("players") or {}).values():
        if p.get("team") != team or p.get("status") not in RESERVE_OR_PS:
            continue
        if skill_only and p.get("pos") not in SKILL:
            continue
        rows.append({
            # No "since": for a player carried from the nflverse baseline it is the
            # baseline's date, not when he went on the list.
            "Player": p.get("name", ""), "Pos": p.get("pos", ""), "Status": p.get("status", ""),
            "Eligible Wk": p.get("earliest_return_week") or None,
            "Elevations": p.get("elevations_used") or 0,
        })
    order = {s: i for i, s in enumerate(RESERVE_OR_PS)}
    rows.sort(key=lambda r: (order.get(r["Status"], 9), r["Pos"] not in SKILL, r["Player"]))
    return rows


_ACTIVE_POS_ORDER = {p: i for i, p in enumerate(("QB", "RB", "FB", "WR", "TE", "K",
                                                 "C", "G", "OG", "T", "OT", "OL"))}


def active_roster_rows(state: Optional[dict], team: str, skill_only: bool = False) -> list[dict]:
    """The club's active roster (nflverse ``ACT``), skill positions first.

    The Roster section used to show only ``roster_rows`` - who is *off* the 53 -
    under a heading that reads like the whole roster, so "who is actually on
    this team right now" was the one roster question the Team page could not
    answer. The data was already in ``state.json`` (every player nflverse
    carries, with a status); this just surfaces it.
    """
    rows = []
    for p in ((state or {}).get("players") or {}).values():
        if p.get("team") != team or p.get("status") != "ACT":
            continue
        pos = p.get("pos", "")
        if skill_only and pos not in SKILL:
            continue
        rows.append({
            "Player": p.get("name", ""), "Pos": pos,
            # An elevated practice-squad player reads ACT for the week and
            # reverts on his own, so the count is worth carrying here too.
            "Elevations": p.get("elevations_used") or 0,
        })
    rows.sort(key=lambda r: (_ACTIVE_POS_ORDER.get(r["Pos"], 99), r["Pos"], r["Player"]))
    return rows


def event_rows(events: list[dict], team: str, since: Optional[str] = None) -> list[dict]:
    """Roster events for ``team`` on/after ``since`` (ISO date), newest first.

    OurLads rows that only say a name left or joined a list are left out —
    the official, nflverse and news rows carry the same moves with a type.
    """
    rows = []
    for e in events or []:
        if team not in (e.get("team"), e.get("to_team"), e.get("from_team")):
            continue
        if since and str(e.get("date") or "") < since:
            continue
        if e.get("source_kind") == "ourlads" and e.get("event_type") == "status_change":
            continue
        rows.append({
            "Date": e.get("date", ""), "Player": e.get("name", ""), "Pos": e.get("pos", ""),
            "Move": (e.get("event_type") or "").replace("_", " "),
            "Detail": (e.get("detail") or "")[:80],
            "Source": (e.get("source") or "").replace("news:", ""),
            "Confidence": e.get("confidence", ""),
        })
    return rows


ELEVATION_DEADLINE = ("Standard elevations are declared by **4:00 PM ET the day before the game** "
                      "(Saturday for Sunday, Wednesday for Thursday, Sunday for Monday) and an "
                      "elevated player is active for it. Each club may elevate up to two a game, "
                      "three times per player per season — a club with none simply elevated nobody.")


def elevation_rows(events: list[dict], state: Optional[dict], max_elevations: int = 3) -> list[dict]:
    """One row per elevation, newest first, with the player's season count."""
    by_gsis = (state or {}).get("players") or {}
    by_name = (state or {}).get("by_name") or {}
    rows = []
    for e in events or []:
        gid = e.get("gsis_id") or by_name.get(e.get("name_key") or "")
        used = (by_gsis.get(gid) or {}).get("elevations_used") if gid else None
        rows.append({
            "Date": e.get("date", ""), "Team": e.get("team", ""), "Player": e.get("name", ""),
            "Pos": e.get("pos", ""),
            "Used": f"{used}/{max_elevations}" if used else "",
            "Source": (e.get("source") or "").replace("news:", ""),
            "Confidence": e.get("confidence", ""),
        })
    rows.sort(key=lambda r: (r["Date"], r["Team"], r["Player"]), reverse=True)
    return rows


def elevation_status(events: list[dict], schedule: list[dict], week: int, today: str) -> dict:
    """Are this week's elevations all in? Counts, and which clubs with a game
    still to play have none recorded."""
    from processing.season import games_for_week

    reported = {e.get("team") for e in events or []}
    upcoming: set[str] = set()
    for g in games_for_week(schedule or [], week):
        if str(g.get("date") or "") < today:
            continue                      # game played: its elevations are settled
        for side in ("home", "away"):
            if g.get(side):
                upcoming.add(to_news(g[side], "proj"))
    return {
        "total": len(events or []),
        "today": sum(1 for e in events or [] if e.get("date") == today),
        "teams": len(reported),
        "waiting": sorted(upcoming - reported),
        "upcoming_teams": len(upcoming),
    }


def audit_alerts(audit: Optional[dict], team: str) -> list[dict]:
    """Open audit alerts for ``team`` (the audit speaks projection style)."""
    out = [a for a in (audit or {}).get("alerts") or []
           if to_news(str(a.get("team") or ""), "proj") == team]
    rank = {"error": 0, "warning": 1, "info": 2}
    out.sort(key=lambda a: rank.get(a.get("severity", ""), 3))
    return out


# Weekly sheet output stat -> column, in display order.
PROJ_STATS = [
    ("PYards", "Pass Yds"), ("PTDs", "Pass TD"), ("INTs", "INT"),
    ("Des RuAtt", "Rush Att"), ("Des RuYds", "Rush Yds"), ("Des RuTD", "Rush TD"),
    ("TGTs", "Tgts"), ("RECs", "Rec"), ("REC Yds", "Rec Yds"), ("Rec TD", "Rec TD"),
]


def projection_rows(output: Optional[dict], players: Optional[dict], team: str,
                    previous_output: Optional[dict] = None, min_ppr: float = 0.1) -> list[dict]:
    """This week's projections for ``team`` from the weekly sheet snapshot,
    with the PPR change since ``previous_output`` (the week's prior snapshot).

    Rows projected for less than ``min_ppr`` are left out (depth bodies the
    sheet carries at zero)."""
    proj_team = to_proj(team)
    prev = previous_output or {}
    rows = []
    for gid, o in (output or {}).items():
        if o.get("team") != proj_team:
            continue
        ppr = o.get("ppr") or 0.0
        if ppr < min_ppr:
            continue
        info = (players or {}).get(gid) or {}
        stats = o.get("stats") or {}
        old = (prev.get(gid) or {}).get("ppr")
        row: dict[str, Any] = {
            "Player": o.get("name", ""), "Pos": o.get("pos", ""), "Depth": info.get("depth"),
            "Status": info.get("status", ""), "PPR": ppr, "Pos rank": o.get("pos_rank", ""),
            "Δ PPR": round(ppr - old, 1) if old is not None and round(ppr - old, 1) else None,
        }
        for key, label in PROJ_STATS:
            v = stats.get(key)
            row[label] = round(v, 1) if v else None
        rows.append(row)
    rows.sort(key=lambda r: -r["PPR"])
    return rows


def readable_name(name: str) -> str:
    """OurLads prints some players in capitals ("ALVIN KAMARA"); title-case
    those, keeping short all-caps tokens ("DJ", "CJ", "II") as they are."""
    if not name or not name.isupper():
        return name
    return " ".join(t if len(t) <= 2 or t in ("III", "IV") else t.title() for t in name.split())


def depth_chart(depth: Optional[dict], team: str,
                positions: tuple[str, ...] = ("QB", "RB", "WR", "TE", "K")) -> list[dict]:
    """OurLads depth for ``team`` at the skill positions: one row per
    OurLads slot (LWR / RWR / SWR ...), players in depth order."""
    slots: dict[str, list[tuple[int, str]]] = {}
    for p in (depth or {}).values():
        if to_news(str(p.get("team") or ""), "ourlads") != team:
            continue
        if p.get("generic_pos") not in positions:
            continue
        slots.setdefault(p.get("pos", ""), []).append((p.get("depth") or 99, readable_name(p.get("name", ""))))
    order = {pos: i for i, pos in enumerate(positions)}
    generic = {p.get("pos"): p.get("generic_pos") for p in (depth or {}).values()}
    rows = []
    for slot in sorted(slots, key=lambda s: (order.get(generic.get(s), 9), s)):
        names = [n for _d, n in sorted(slots[slot])]
        row = {"Slot": slot}
        for i, n in enumerate(names[:4], start=1):
            row[f"#{i}"] = n
        rows.append(row)
    return rows
