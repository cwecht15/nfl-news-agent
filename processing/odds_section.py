"""Build the Line Movement report section.

Turns the typed changes in ``data/odds/<season>/wkNN.json`` into a report
section, and — the point of the exercise — pairs each move with the day's
news for the same team or player, so a line that moved sits next to the
report that moved it.

Two layers, deliberately in this order:

1. **Deterministic pairing.** Every mover is joined to the day's news items
   (team tag for game lines; normalized player name in the title/body for
   props) and to the same day's injury-report changes, roster events and
   declared inactives. This is what carries the section — it is always
   correct and costs nothing.
2. **An optional LLM lede** — one small call that reads the paired list and
   writes 2-4 bullets on what actually happened. Off in the afternoon run
   (no LLM there) and skippable via ``odds.report.llm_lede``. If it fails,
   the section renders exactly as it would have without it.

Shape matches ``processing.fp_section``: ``{"summary", "numbered_sources",
"sources", "count"}`` so the dashboard's ``[N]`` linkifier and the standalone
HTML footer both work with no special-casing.
"""

from __future__ import annotations

import logging
import re
import sys
from pathlib import Path
from typing import Any, Optional

PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config_loader import get_settings

logger = logging.getLogger(__name__)

# Types that describe a market move, in render order, with their headings.
MOVE_SECTIONS = [
    ("spread_move", "Spreads"),
    ("total_move", "Totals"),
    ("ml_move", "Moneylines"),
    ("prop_move", "Player props"),
    ("prop_new", "New player lines"),
    ("sheet_drift", "Sheet vs market"),
    ("market_only", "Quoted, not projected"),
]

# Three budget groups. `odds.report.max_games` / `max_props` bound the raw
# movers; the state flags (sheet drift, quoted-not-projected) are the most
# actionable rows in the section and get their own allowance, so a Sunday where
# the whole slate moves can never crowd them out.
GAME_MOVE_TYPES = {"spread_move", "total_move", "ml_move"}
PROP_MOVE_TYPES = {"prop_move", "prop_new"}
STATE_TYPES = {"sheet_drift", "market_only"}

# Ceiling per type within its group.
PER_TYPE_CAP = {
    "spread_move": 8, "total_move": 8, "ml_move": 5,
    "prop_move": 15, "prop_new": 6, "sheet_drift": 8, "market_only": 6,
}

# Slots each type is guaranteed before the rest of its group's budget is
# handed out by magnitude. Without this, spread (8) + total (8) exactly consume
# a 16-game budget and moneylines never render.
PER_TYPE_RESERVE = 3

MAX_OUTPUT_TOKENS = 900

LEDE_PROMPT = """You are an NFL fantasy analyst writing the opening of a "Line Movement"
section in a daily report. Below is today's list of betting-market moves, each already
paired with the news items from the same day that mention the same team or player.

Write 2-4 short bullets covering ONLY the moves that matter for fantasy projections.
For each one, say what moved and what the paired news suggests is behind it.

Rules:
- Lead each bullet with the bolded subject (**Player Name** or **AWAY@HOME**).
- Cite the news you used with its [N] marker, exactly as it appears below. Never
  invent a citation number.
- If a move has no paired news, you may still cover it — say the move is unexplained
  rather than inventing a cause. Do NOT assert causation the pairing doesn't support.
- Skip anything that is only a restatement of the number itself.
- No header, no intro, no trailer. Output only the bullet list.
"""


def _cfg(settings: Optional[dict] = None) -> dict:
    return ((settings or get_settings()).get("odds", {}) or {}).get("report", {}) or {}


def _name_key(name: str) -> str:
    from processing.sheet_reconciliation import _normalize_name
    return _normalize_name(name or "")


def _last_name(name: str) -> str:
    parts = [p for p in _name_key(name).split() if p]
    return parts[-1] if parts else ""


def _teams_of(change: dict, games: dict) -> set[str]:
    """Every team a change concerns — both clubs for a game-level move."""
    teams: set[str] = set()
    if change.get("team"):
        teams.add(change["team"])
    g = games.get(change.get("game") or "")
    if g:
        teams.update(x for x in (g.get("home"), g.get("away")) if x)
    return teams


def _news_haystacks(news_items: list) -> list[tuple[Any, str, set]]:
    """(item, normalized searchable text, teams) once per item.

    Bodies run to ~8KB and ``_normalize_name`` is a regex chain, so normalizing
    inside the per-change loop would redo the same work for every mover.
    Non-alphanumerics collapse to spaces so a surname followed by a comma or a
    period still matches on a word boundary.
    """
    out = []
    for item in news_items:
        raw = " ".join(str(x or "") for x in (
            getattr(item, "title", ""), getattr(item, "summary", ""),
            getattr(item, "full_text", ""),
        ))
        hay = re.sub(r"[^a-z0-9]+", " ", _name_key(raw))
        out.append((item, f" {hay} ", set(getattr(item, "teams", []) or [])))
    return out


def _match_news(change: dict, games: dict, haystacks: list[tuple[Any, str, set]]) -> list:
    """News items that plausibly concern this change.

    Player moves need the player actually named — a team tag alone would
    attach every story about the club to every one of its props.
    """
    teams = _teams_of(change, games)
    player = change.get("player") or ""
    if player:
        key, last = _name_key(player), _last_name(player)
        if not last:
            return []
        hits = []
        for item, hay, item_teams in haystacks:
            if key and f" {key} " in hay:
                hits.append(item)
            elif last and f" {last} " in hay:
                # A bare last name is only trusted when the club matches.
                if not teams or (item_teams & teams):
                    hits.append(item)
        return hits
    if not teams:
        return []
    return [item for item, _hay, item_teams in haystacks if item_teams & teams]


def _match_events(change: dict, games: dict, injury_changes, roster_events, inactives) -> list[str]:
    """Same-day pipeline evidence (structured, no URL) for this move.

    For a player move the player's own rows come first, but the *teammates*
    matter just as much: a backup's rushing line jumps because the starter was
    ruled out. So same-team OUT designations and declared inactives at a skill
    position are included too, labelled as teammate context so nothing reads as
    though it happened to the player who moved.
    """
    teams = _teams_of(change, games)
    player = change.get("player") or ""
    key = _name_key(player)
    pos = str(change.get("pos") or "")
    skill = {"QB", "RB", "WR", "TE"}
    own: list[str] = []
    mates: list[tuple[int, str]] = []   # (rank, text) — same position first

    for c in injury_changes or []:
        c_team, c_name = c.get("team"), str(c.get("name") or "")
        msg = c.get("message") or c.get("type")
        if key and _name_key(c_name) == key:
            own.append(f"injury report: {msg}")
        elif c_team in teams and str(c.get("new") or "").upper() in ("OUT", "D", "DOUBTFUL"):
            mates.append((0 if str(c.get("pos") or "") == pos else 1, f"teammate: {msg}"))

    for e in roster_events or []:
        e_name = str(e.get("name") or "")
        msg = e.get("message") or e.get("event_type")
        if key and _name_key(e_name) == key:
            own.append(f"roster: {msg}")
        elif not player and e.get("team") in teams:
            mates.append((1, f"roster: {msg}"))

    for (team, name_key), row in (inactives or {}).items():
        if key and name_key == key:
            own.append(f"inactive: {row.get('name')} ({row.get('pos')}) declared inactive")
        elif team in teams and row.get("pos") in skill:
            # A backup QB being inactive says nothing about a running back's
            # line; a same-position teammate says everything.
            if player and str(row.get("pos") or "") != pos:
                continue
            mates.append((0, f"teammate inactive: {row.get('name')} ({row.get('pos')}, {team})"))

    ordered = own + [t for _r, t in sorted(mates, key=lambda m: m[0])]
    seen, uniq = set(), []
    for line in ordered:
        if line not in seen:
            seen.add(line)
            uniq.append(line)
    return uniq[:3]


def _fmt(v) -> str:
    if v is None:
        return "?"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    return f"{f:.1f}" if abs(f) >= 1 else f"{f:.2f}"


def _group_player_moves(changes: list[dict]) -> list[dict]:
    """One bullet per player, not per stat.

    The market quotes a back across Rush Att / Rush Yds / Rush+Rec Yds and they
    all move together, so three near-identical bullets (each repeating the same
    paired news) is noise. Merge them, keeping the largest move as the ranking
    magnitude.
    """
    out: list[dict] = []
    groups: dict[str, dict] = {}
    for c in changes:
        if c.get("type") not in ("prop_move", "prop_new") or not c.get("player"):
            out.append(c)
            continue
        key = f"{c.get('type')}|{c.get('gsis_id') or c.get('player')}"
        g = groups.get(key)
        if g is None:
            groups[key] = g = dict(c, _stats=[])
        g["_stats"].append(c)
        g["magnitude"] = max(float(g.get("magnitude") or 0), float(c.get("magnitude") or 0))

    for g in groups.values():
        stats = sorted(g.pop("_stats"), key=lambda c: -float(c.get("magnitude") or 0))
        if len(stats) == 1:
            out.append(stats[0])
            continue
        head = stats[0]
        is_new = head.get("type") == "prop_new"
        bits = []
        for c in stats:
            label = c.get("stat_label") or c.get("stat")
            # A new line has no "before" — "Rec ? → 1.2" is worse than "Rec 1.2".
            piece = (f"{label} {_fmt(c.get('new'))}" if is_new
                     else f"{label} {_fmt(c.get('old'))} → {_fmt(c.get('new'))}")
            if c.get("ours") is not None:
                piece += f" (we project {_fmt(c['ours'])})"
            bits.append(piece)
        tail = "new lines this week" if is_new else head.get("basis", "")
        g["message"] = (f"**{head.get('player')}** ({head.get('pos', '')}, "
                        f"{head.get('team', '')}) — " + "; ".join(bits)
                        + (f" — {tail}" if tail else ""))
        g["stat"] = head.get("stat")
        g["stat_label"] = ", ".join(c.get("stat_label") or c.get("stat") for c in stats)
        out.append(g)
    return out


def _budget_group(ctype: str) -> str:
    if ctype in GAME_MOVE_TYPES:
        return "games"
    if ctype in PROP_MOVE_TYPES:
        return "props"
    return "state"


def _select(changes: list[dict], settings: Optional[dict] = None) -> list[dict]:
    """Rank within each type, then hand out each budget group's slots.

    Two passes so a busy category cannot starve a quieter one: every type first
    gets up to ``PER_TYPE_RESERVE`` of its biggest rows, then whatever is left
    of the group's budget goes to the largest remaining movers regardless of
    type. Returns copies — the caller annotates them, and the originals are
    what gets persisted on the report.
    """
    cfg = _cfg(settings)
    budgets = {
        "games": int(cfg.get("max_games", 16)),
        "props": int(cfg.get("max_props", 15)),
        # State flags report once and then only on change, so their own per-type
        # caps are the only bound they need.
        "state": sum(PER_TYPE_CAP.get(t, 10) for t in STATE_TYPES),
    }

    by_type: dict[str, list[dict]] = {}
    for c in _group_player_moves(changes):
        by_type.setdefault(c.get("type", ""), []).append(c)
    for rows in by_type.values():
        rows.sort(key=lambda c: -float(c.get("magnitude") or 0))

    taken: dict[str, int] = {}
    picked_ids: set[int] = set()
    picked: list[dict] = []

    def _take(c: dict, ctype: str) -> bool:
        group = _budget_group(ctype)
        if budgets[group] <= 0 or taken.get(ctype, 0) >= PER_TYPE_CAP.get(ctype, 10):
            return False
        budgets[group] -= 1
        taken[ctype] = taken.get(ctype, 0) + 1
        picked_ids.add(id(c))
        picked.append(c)
        return True

    for ctype, _label in MOVE_SECTIONS:                      # pass 1: reserve
        for c in by_type.get(ctype, [])[:PER_TYPE_RESERVE]:
            _take(c, ctype)

    remainder = [(c, t) for t, rows in by_type.items() for c in rows if id(c) not in picked_ids]
    remainder.sort(key=lambda pair: -float(pair[0].get("magnitude") or 0))
    for c, ctype in remainder:                               # pass 2: by magnitude
        _take(c, ctype)

    order = {t: i for i, (t, _l) in enumerate(MOVE_SECTIONS)}
    picked.sort(key=lambda c: (order.get(c.get("type", ""), 99),
                               -float(c.get("magnitude") or 0)))
    return [dict(c) for c in picked]


def _stale_note(pull: dict) -> str:
    reason = (pull or {}).get("stale_reason")
    at = (pull or {}).get("pulled_at")
    if reason:
        return f"_Lines are stale — {reason}._"
    if at:
        return f"_Lines as of the {at.replace('T', ' ')} odds pull._"
    return "_Odds pull time unknown._"


def build_odds_section(
    week_data: Optional[dict],
    news_items: Optional[list] = None,
    *,
    injury_changes: Optional[list[dict]] = None,
    roster_events: Optional[list[dict]] = None,
    inactives: Optional[dict] = None,
    client: Optional[Any] = None,
    usage_tracker: Optional[dict[str, Any]] = None,
    settings: Optional[dict] = None,
    date_label: str = "",
    use_llm: bool = True,
) -> Optional[dict[str, Any]]:
    """Render the Line Movement section, or None when there is nothing to show.

    ``client=None`` with ``use_llm=True`` resolves a client from config; the
    afternoon run passes ``use_llm=False`` because it makes no LLM calls.
    """
    if not week_data:
        return None
    settings = settings or get_settings()
    games = week_data.get("games") or {}
    pull = week_data.get("pull") or {}
    changes = list(week_data.get("changes") or [])
    news_items = list(news_items or [])

    if not changes:
        # "No movement" and "we could not read the market" are different
        # statements; a stale pull must not be reported as a quiet day.
        note = _stale_note(pull)
        summary = note if pull.get("stale_reason") else (
            "No market movement past the reporting thresholds.\n\n" + note)
        return {"summary": summary, "count": 0, "sources": [], "numbered_sources": []}

    picked = _select(changes, settings)

    # --- deterministic pairing --------------------------------------------
    numbered: list[dict[str, Any]] = []
    by_url: dict[str, int] = {}

    def _cite(item) -> int:
        # A blank url would collapse every such item onto one [N] carrying the
        # first one's title, so fall back to title+source as the identity.
        title = getattr(item, "title", "") or ""
        source = getattr(item, "source", "") or getattr(item, "source_type", "") or ""
        url = getattr(item, "url", "") or ""
        ident = url or f"{title}|{source}"
        if ident in by_url:
            return by_url[ident]
        num = len(numbered) + 1
        by_url[ident] = num
        numbered.append({
            "num": num,
            "title": title,
            "url": url,
            "source": source,
            "published": (getattr(item, "published", None).isoformat()
                          if getattr(item, "published", None) else ""),
        })
        return num

    haystacks = _news_haystacks(news_items)
    for c in picked:
        matched = _match_news(c, games, haystacks)[:3]
        c["_cites"] = [_cite(i) for i in matched]
        c["_events"] = _match_events(c, games, injury_changes, roster_events, inactives)

    # --- markdown ---------------------------------------------------------
    parts: list[str] = [_stale_note(pull), ""]
    for ctype, label in MOVE_SECTIONS:
        rows = [c for c in picked if c.get("type") == ctype]
        if not rows:
            continue
        parts.append(f"### {label}")
        for c in rows:
            line = f"- {c.get('message', '')}"
            tail: list[str] = []
            if c["_events"]:
                tail.append("; ".join(c["_events"]))
            if c["_cites"]:
                tail.append(" ".join(f"[{n}]" for n in c["_cites"]))
            if tail:
                line += " — " + " ".join(tail)
            parts.append(line)
        parts.append("")

    body = "\n".join(parts).strip()

    # --- optional LLM lede ------------------------------------------------
    lede = ""
    if use_llm and bool(_cfg(settings).get("llm_lede", True)):
        lede = _build_lede(picked, numbered, client, usage_tracker, date_label)

    summary = (f"{lede}\n\n{body}" if lede else body)

    return {
        "summary": summary,
        "numbered_sources": numbered,
        "sources": numbered,
        "count": len(picked),
    }


def _build_lede(picked: list[dict], numbered: list[dict], client, usage_tracker,
                date_label: str) -> str:
    """One small call over the paired movers. Never raises."""
    if not picked:
        return ""
    try:
        from processing.summarizer import (
            _call_model, _init_usage_tracker, _resolve_client_and_runtime,
        )

        client, runtime = _resolve_client_and_runtime(client)
        if usage_tracker is None:
            usage_tracker = _init_usage_tracker(runtime)
        section_cfg = (runtime.get("sections", {}) or {}).get("line_movement", {}) or {}

        lines = []
        for c in picked[:25]:
            cites = " ".join(f"[{n}]" for n in c.get("_cites") or [])
            evidence = "; ".join(c.get("_events") or [])
            lines.append(
                f"- ({c['type']}) {c.get('message', '')}"
                + (f" | same-day pipeline evidence: {evidence}" if evidence else "")
                + (f" | news: {cites}" if cites else " | news: none")
            )
        src = "\n".join(f"[{s['num']}] {s['title']} ({s['source']})" for s in numbered)
        prompt = (LEDE_PROMPT + "\n\nMoves:\n" + "\n".join(lines)
                  + ("\n\nNews items available for citation:\n" + src if src else ""))

        out = _call_model(
            client, prompt, runtime,
            max_tokens=int(section_cfg.get("max_output_tokens", MAX_OUTPUT_TOKENS)),
            usage_tracker=usage_tracker,
            usage_label="line_movement_section",
            reasoning_effort=section_cfg.get("reasoning_effort", "low"),
            model=section_cfg.get("model"),
        )
        out = (out or "").strip()
        if out:
            logger.info("Line Movement lede: %d chars (%s)", len(out), date_label or "no label")
        return out
    except Exception as e:  # noqa: BLE001 — the lede is a bonus, never a blocker
        logger.warning("Line Movement lede failed (section still renders): %s", e)
        return ""
