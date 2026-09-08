"""Roster events ledger + derived roster state (in-season).

Why: in-season the highest-value signal is *roster status* — who is on IR
(and when they can come back), who is on PUP/NFI/suspended, who is on the
practice squad and how many standard elevations they've burned. No single
source has all of it, and none of them is both fast and official:

* **NFL.com transactions** (``collectors/web_scraper``) are official but
  never post standard elevations, IR activations or designated-to-return.
* **nflverse** (``collectors/nflverse_roster_collector``) is a GSIS-keyed
  daily baseline; day-over-day status flips reveal elevations/activations,
  but it lags official moves by up to a day and can't say *why* a player
  went DEV -> ACT -> DEV (elevation vs. promotion + release).
* **OurLads** reserve buckets (``depth_chart_collector.split_reserve_changes``)
  are a slow but independent confirmation of IR/PUP/NFI/SUS.
* **Insider tweets / news** are fastest but unofficial.

So every source is normalized into one *event* shape, appended to an
append-only ledger (``data/roster/events.jsonl``), and the ledger is
replayed over the nflverse baseline into ``data/roster/state.json`` on
every run. Reported (news) events are applied immediately when
``roster.apply_reported_events`` is on and get ``confirmed_by`` once an
official / nflverse / OurLads event for the same player + event family
lands inside ``roster.confirm_window_days``.

Event record::

    {event_id, date, season, week, observed_at, gsis_id|None, name, name_key,
     team, from_team, to_team, pos, event_type, detail, source, source_kind,
     confidence, url, confirmed_by}

``source_kind`` ∈ official | nflverse | ourlads | news;
``confidence`` ∈ official | confirmed | reported.

CLI::

    python -m processing.roster_events --rebuild [--date YYYY-MM-DD]
    python -m processing.roster_events --classify "Bills placed RB Ray Davis on IR"
    python -m processing.roster_events --backfill 2026-08-26 [--end 2026-09-07]
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config_loader import get_data_dir, get_settings, get_teams
from collectors.nflverse_roster_collector import (
    diff_nflverse,
    latest_nflverse_snapshot,
    name_key as _name_key,
    status_label,
)
from processing.season import (
    earliest_return_week,
    get_season_year,
    load_schedule,
    week_from_date,
)
from processing.team_abbr import _nickname_index, to_news, to_proj
from scripts.transaction_reconciler import _extract_player_name

logger = logging.getLogger(__name__)

NFLCOM_SOURCE = "NFL.com Transactions"

EVENT_TYPES: frozenset[str] = frozenset({
    "ir_placed", "ir_designated_return", "ir_activated",
    "pup_placed", "pup_activated",
    "nfi_placed", "nfi_activated",
    "suspended", "reinstated", "exempt",
    "ps_signed", "ps_released", "ps_elevated", "ps_promoted",
    "waived", "released", "claimed", "signed", "traded",
    "injury_settlement", "retired", "team_change", "status_change",
})

STATUS_VOCAB = ("ACT", "PS", "IR", "PUP", "NFI", "SUS", "EXE", "RET", "FA", "UNKNOWN")

_CONFIDENCE_RANK = {"reported": 0, "confirmed": 1, "official": 2}

# ---------------------------------------------------------------------------
# NFL.com transaction descriptions
# ---------------------------------------------------------------------------

# Exact description strings observed in the feed (last season + this month).
NFLCOM_DESCRIPTION_MAP: dict[str, str] = {
    "Free Agent Signing": "signed",
    "Practice Squad": "ps_signed",
    "Practice Squad Veteran": "ps_signed",
    "Waived, No Recall": "released",
    "Terminated Via Waivers, all contracts": "released",
    "Terminated, Vested Veteran, all contracts": "released",
    "Waived, Injured, Prior to Cut to 53": "waived",
    "Waived, Injury Settlement": "injury_settlement",
    "Reserve/Injured": "ir_placed",
    "Reserve/Injured from Waived/Injured": "ir_placed",
    "Reserve/Injured from Waived/Injured; Not Against 90": "ir_placed",
    "Reserve/Physically Unable to Perform": "pup_placed",
    "Reserve/Retired": "retired",
    "Reserve/Suspended By Commissioner-Less than One Year": "suspended",
    "Suspension Lifted by Commissioner": "reinstated",
    "Exempt/Commissioner Permission": "exempt",
    "Traded": "traded",
    "Assigned via Waivers": "claimed",
}

_NFLCOM_MAP_CI = {k.lower(): v for k, v in NFLCOM_DESCRIPTION_MAP.items()}

# Prefix / regex fallbacks for suffix variants we haven't seen verbatim.
# Order matters: more specific first.
_NFLCOM_FALLBACK: list[tuple[re.Pattern, str]] = [
    (re.compile(r"designated (?:for|to) return", re.I), "ir_designated_return"),
    (re.compile(r"^activated", re.I), "ir_activated"),
    (re.compile(r"elevat", re.I), "ps_elevated"),
    (re.compile(r"^terminated", re.I), "released"),
    (re.compile(r"^waived,?\s*no recall", re.I), "released"),
    (re.compile(r"^waived,?\s*injur(?:y|ed) settlement", re.I), "injury_settlement"),
    (re.compile(r"^waived,?\s*injured", re.I), "waived"),
    (re.compile(r"^waived", re.I), "waived"),
    (re.compile(r"^reserve/injured", re.I), "ir_placed"),
    (re.compile(r"^reserve/physically", re.I), "pup_placed"),
    (re.compile(r"^reserve/non-?football", re.I), "nfi_placed"),
    (re.compile(r"^reserve/retired", re.I), "retired"),
    (re.compile(r"^reserve/suspended", re.I), "suspended"),
    (re.compile(r"^suspension lifted|reinstat", re.I), "reinstated"),
    (re.compile(r"^exempt", re.I), "exempt"),
    (re.compile(r"^traded", re.I), "traded"),
    (re.compile(r"^assigned via waivers|claimed", re.I), "claimed"),
    (re.compile(r"^practice squad", re.I), "ps_signed"),
    (re.compile(r"free agent signing|^signed|signing", re.I), "signed"),
]

_warned_tx_types: set[str] = set()


def classify_nflcom(tx_type: str) -> str:
    """NFL.com description string -> event type.

    Exact match first, then case-insensitive exact, then prefix/regex
    fallbacks. Unknown strings become ``status_change`` and are logged once
    so new wording surfaces in the Week 1 logs instead of vanishing.
    """
    raw = (tx_type or "").strip()
    if not raw:
        return "status_change"
    if raw in NFLCOM_DESCRIPTION_MAP:
        return NFLCOM_DESCRIPTION_MAP[raw]
    low = re.sub(r"\s+", " ", raw.lower())
    if low in _NFLCOM_MAP_CI:
        return _NFLCOM_MAP_CI[low]
    for pat, etype in _NFLCOM_FALLBACK:
        if pat.search(raw):
            return etype
    if raw not in _warned_tx_types:
        _warned_tx_types.add(raw)
        logger.warning("Unknown NFL.com transaction description %r -> status_change", raw)
    return "status_change"


# ---------------------------------------------------------------------------
# News / tweet title classifier
# ---------------------------------------------------------------------------

# Every pattern names its verb as group ``v`` so the player extractor can
# look immediately after (active voice: "Bills placed RB Ray Davis on IR")
# or immediately before it (passive / subject-first: "Ray Davis was placed on IR").
# A clause-local gap: no sentence punctuation, but initials ("T.J."),
# "St. Brown" and "Jr." may cross it.
_GAP = r"(?:[^.;:!?\n]|\.(?=\S)|(?<=[A-Z])\.|(?<=St)\.|(?<=Jr)\.|(?<=Sr)\.){0,60}?"
_IR = r"(?:injured reserve list|injured reserve|reserve/injured|IR(?![A-Za-z]))"
_PUP = r"(?:PUP(?![A-Za-z])|physically unable to perform(?: list)?)"
_NFI = r"(?:NFI(?![A-Za-z])|non-?football (?:injury|illness)(?: list)?)"
_PS = r"practice[- ]squad"
# "cut" as a roster verb only — not "roster cut", "final cuts", "work cut out".
_CUT = r"(?<!roster )(?<!Roster )(?<!final )(?<!Final )cut(?!s\b)(?!\s+(?:out|down|ties|back|off|the cord|to \d))"
_PLACE_V = r"(?P<v>plac(?:e|ed|es|ing)|put(?:s|ting)?|mov(?:ed|es|ing)|head(?:ed|s|ing)|go(?:es|ing)|land(?:s|ed|ing)|sen[dt](?:s|ing)?|shut down)"

NEWS_PATTERNS: list[tuple[re.Pattern, str]] = [
    # --- designated to return / 21-day window ---
    (re.compile(rf"\b(?P<v>designat(?:e|ed|es|ing))\b{_GAP}\b(?:to|for) return\b", re.I), "ir_designated_return"),
    (re.compile(rf"\b(?P<v>open(?:s|ed|ing)?|start(?:s|ed|ing)?|begin(?:s|ning)?|began|trigger(?:s|ed))\b{_GAP}\b21-day (?:practice )?window\b", re.I), "ir_designated_return"),
    (re.compile(rf"\b(?P<v>return(?:s|ed|ing)?) to practice\b{_GAP}\b(?:from|off) (?:the )?(?:{_IR}|{_PUP}|{_NFI})", re.I), "ir_designated_return"),
    # --- activations ---
    (re.compile(rf"\b(?P<v>activat(?:e|ed|es|ing))\b{_GAP}\b(?:from|off) (?:the )?{_IR}", re.I), "ir_activated"),
    (re.compile(rf"\b(?P<v>activat(?:e|ed|es|ing))\b{_GAP}\b(?:from|off) (?:the )?{_PUP}", re.I), "pup_activated"),
    (re.compile(rf"\b(?P<v>activat(?:e|ed|es|ing))\b{_GAP}\b(?:from|off) (?:the )?{_NFI}", re.I), "nfi_activated"),
    # --- placements ---
    (re.compile(rf"\b{_PLACE_V}\b{_GAP}\b(?:on|to) (?:the )?{_IR}", re.I), "ir_placed"),
    (re.compile(rf"\b{_PLACE_V}\b{_GAP}\b(?:on|to) (?:the )?{_PUP}", re.I), "pup_placed"),
    (re.compile(rf"\b{_PLACE_V}\b{_GAP}\b(?:on|to) (?:the )?{_NFI}", re.I), "nfi_placed"),
    (re.compile(r"\b(?P<v>season[- ]ending|out for the (?:season|year)|done for the (?:season|year)|lost for the (?:season|year))\b", re.I), "ir_placed"),
    # --- practice squad ---
    (re.compile(rf"\b(?P<v>elevat(?:e|ed|es|ing))\b{_GAP}\b{_PS}\b", re.I), "ps_elevated"),
    (re.compile(rf"\b{_PS}\b{_GAP}\b(?P<v>elevat(?:e|ed|es|ing|ion))\b", re.I), "ps_elevated"),
    (re.compile(r"\b(?P<v>standard elevation)s?\b", re.I), "ps_elevated"),
    (re.compile(rf"\b(?P<v>sign(?:s|ed|ing)?|re-sign(?:s|ed|ing)?|promot(?:e|ed|es|ing))\b{_GAP}\b(?:to|onto) (?:the |their |its |his )?(?:active|53-man|53 man) roster\b", re.I), "ps_promoted"),
    (re.compile(rf"\b(?P<v>promot(?:e|ed|es|ing))\b{_GAP}\b(?:from|off) (?:the |their |its )?{_PS}\b", re.I), "ps_promoted"),
    (re.compile(rf"\b(?P<v>sign(?:s|ed|ing)?)\b{_GAP}\b(?:off|from) (?:the |their |its )?(?:[A-Z#][\w'’]*\s+)?{_PS}\b", re.I), "ps_promoted"),
    (re.compile(rf"\b(?P<v>releas(?:e|ed|es|ing)|{_CUT}|waiv(?:e|ed|es|ing)|drop(?:ped|s|ping))\b{_GAP}\bfrom (?:the |their |its )?{_PS}\b", re.I), "ps_released"),
    (re.compile(rf"\b(?P<v>releas(?:e|ed|es|ing))\b\s+(?:the |their )?{_PS}\b", re.I), "ps_released"),
    (re.compile(rf"\b(?P<v>sign(?:s|ed|ing)?|re-sign(?:s|ed|ing)?|add(?:s|ed|ing)?|bring(?:s|ing)?|brought)\b{_GAP}\b(?:to|with|onto|via) (?:the |their |its |his )?(?:[\w'’#-]+\s+){{0,2}}?{_PS}\b", re.I), "ps_signed"),
    # --- waivers / cuts ---
    (re.compile(rf"\b(?P<v>claim(?:s|ed|ing)?)\b{_GAP}\b(?:off|via|on|through) (?:the )?waivers\b", re.I), "claimed"),
    (re.compile(r"\b(?P<v>reinstat(?:e|ed|es|ing|ement))\b|\bsuspension (?:has been |was |is )?(?P<v2>lifted)\b", re.I), "reinstated"),
    (re.compile(r"\b(?P<v>suspend(?:s|ed|ing)?)\b", re.I), "suspended"),
    (re.compile(r"\b(?P<v>waiv(?:e|ed|es|ing))\b", re.I), "waived"),
    (re.compile(rf"\b(?P<v>releas(?:e|ed|es|ing)|{_CUT})\b", re.I), "released"),
]

# Words right before a match that turn it into speculation / negation.
_NEGATION_RE = re.compile(
    r"\b(?:not|no|never|isn['’]t|won['’]t|wasn['’]t|aren['’]t|don['’]t|doesn['’]t|didn['’]t|"
    r"avoid(?:s|ed|ing)?|dodg(?:es|ed)|escap(?:es|ed)|unlikely|instead of|rather than|without|"
    r"if|whether|could|might|may|should|would|can|cannot|can['’]t|fear(?:s|ed)?|hop(?:es|ing)|hope|"
    r"eligible to|able to|probably|possibly|potentially|likely to|option to|free to)"
    r"\b\W*(?:[\w'’-]+\W+){0,4}$",
    re.I,
)
# Clause-level stale re-reports ("whom the 49ers waived yesterday").
_STALE_RE = re.compile(
    r"\b(?:yesterday|last (?:week|month|season|year)|earlier this|previously|had been|whom|who was|"
    r"in (?:january|february|march|april|may|june|july|august)|back in|"
    r"explain(?:s|ed|ing)?|react(?:s|ed|ion)?|thoughts on|recap|ranking|grades?)\b",
    re.I,
)
_CLAUSE_SPLIT = re.compile(r"(?:(?<![A-Z])(?<!Jr)(?<!Sr)(?<!St)(?<!Dr)(?<!Mr)(?<!vs)\.(?=\s|$)|[;!?](?=\s|$)|\n)")


def _clause_bounds(text: str, pos: int) -> tuple[int, int]:
    """(start, end) of the sentence/clause containing ``pos``."""
    start = 0
    for m in _CLAUSE_SPLIT.finditer(text, 0, pos):
        start = m.end()
    m_end = _CLAUSE_SPLIT.search(text, pos)
    end = m_end.end() if m_end else len(text)
    return start, end


def _accept_match(text: str, m: re.Match) -> bool:
    start, end = _clause_bounds(text, m.start())
    clause = text[start:end]
    if clause.rstrip().endswith("?"):
        return False
    if _NEGATION_RE.search(text[start:m.start()]):
        return False
    if _STALE_RE.search(clause):
        return False
    return True


# Bare verbs that show up in non-roster chatter ("releases a statement",
# "cut down to 53") only count when a player name sits next to them.
_NAME_REQUIRED_TYPES = {"released", "waived", "suspended", "reinstated"}
_SETTLEMENT_RE = re.compile(r"\binjury settlement\b", re.I)


def _match_news_all(title: str) -> list[tuple[str, re.Match]]:
    """All accepted (event_type, match) pairs in ``title`` — at most one per
    clause, patterns tried in priority order, results returned in text
    order. ``released`` never fires on a title that talks about the
    practice squad (those are ps_* or nothing)."""
    text = title or ""
    if not text.strip():
        return []
    has_ps = re.search(_PS, text, re.I) is not None
    raw: list[tuple[int, int, str, re.Match]] = []
    for idx, (pat, etype) in enumerate(NEWS_PATTERNS):
        for m in pat.finditer(text):
            if etype == "released" and has_ps:
                continue
            raw.append((_verb_span(m)[0], idx, etype, m))
    # Earliest verb wins a clause ("sign X to the practice squad, released Y"
    # -> ps_signed); same verb position -> the more specific pattern.
    raw.sort(key=lambda r: (r[0], r[1]))
    found: list[tuple[str, re.Match]] = []
    taken: list[tuple[int, int]] = []
    for _pos, _idx, etype, m in raw:
        bounds = _clause_bounds(text, m.start())
        if bounds in taken:
            continue
        if not _accept_match(text, m):
            continue
        if etype in _NAME_REQUIRED_TYPES and not _name_for_match(text, m, etype):
            continue
        if etype in ("waived", "released") and _SETTLEMENT_RE.search(text[bounds[0]:bounds[1]]):
            etype = "injury_settlement"
        found.append((etype, m))
        taken.append(bounds)
    return found


def classify_news(title: str) -> Optional[str]:
    """Event type for a news / tweet title, or None when nothing fires."""
    found = _match_news_all(title)
    return found[0][0] if found else None


# ---------------------------------------------------------------------------
# Player name extraction
# ---------------------------------------------------------------------------

_NAME_TOKEN = r"(?:[A-Z][A-Za-z'’.\-]*[a-z][A-Za-z'’.\-]*|[A-Z]\.?[A-Z]\.?|[A-Z]{2})"
_PARTICLE = r"(?:St\.|Van|De|Da|La|Le|Del|Von|Mc|Mac)"
_SUFFIX = r"(?:Jr\.?|Sr\.?|II|III|IV|V)"
_NAME_CORE = (
    rf"(?P<first>{_NAME_TOKEN})\s+(?:(?P<particle>{_PARTICLE})\s+)?"
    rf"(?P<last>[A-Z][A-Za-z'’\-]*[a-z][A-Za-z'’\-]*)"
    rf"(?:\s+(?P<suffix>{_SUFFIX}))?(?![A-Za-z])"
)
_NAME_RE = re.compile(_NAME_CORE)
_NAME_END_RE = re.compile(_NAME_CORE + r"\.?\s*$")

_POS_ABBR = (
    "QB|RB|FB|WR|TE|OL|OT|OG|G|C|T|DL|DE|DT|NT|EDGE|LB|ILB|OLB|MLB|DB|CB|S|FS|SS|K|P|LS|KR|PR"
)
_FILLER_WORDS = {
    "the", "their", "its", "his", "a", "an", "veteran", "vet", "rookie", "former", "free", "agent",
    "free-agent", "starting", "starter", "backup", "reserve", "injured", "and", "also", "then",
    "newly", "young", "longtime", "undrafted", "first-year", "second-year", "third-year",
    "practice", "squad", "practice-squad", "of", "up",
    # position words (multi-word positions are skipped token by token)
    "quarterback", "running", "back", "fullback", "wide", "receiver", "wideout", "tight", "end",
    "offensive", "defensive", "tackle", "guard", "center", "lineman", "edge", "rusher", "pass",
    "nose", "linebacker", "cornerback", "corner", "safety", "kicker", "punter", "long", "snapper",
    "returner", "player", "players", "specialist",
}
_AUX_TAIL_RE = re.compile(
    r"(?:\s+(?:is|was|were|are|has|have|had|been|being|will|would|to|be|indeed|reportedly|"
    r"officially|now|also|just|already|expected|set|got|gets|getting|likely|then|again|"
    r"formally|quietly|who|that))*\s*[,:]?\s*$",
    re.I,
)
_POSSESSIVE_TEAM_RE = re.compile(r"^#?[A-Z0-9][A-Za-z0-9]+['’]s?$")

_GENERIC_STOP = {
    "report", "reports", "source", "sources", "per", "breaking", "update", "official", "officially",
    "nfl", "afc", "nfc", "big", "ten", "sec", "acc", "league", "week", "team", "teams", "coach",
    "head", "gm", "owner", "commissioner", "the", "and", "for", "with", "from", "practice",
    "squad", "injured", "reserve", "free", "agent", "veteran", "rookie", "monday", "tuesday",
    "wednesday", "thursday", "friday", "saturday", "sunday", "january", "february", "march",
    "april", "june", "july", "august", "september", "october", "november", "december",
    "pro", "bowl", "super", "draft", "camp", "training", "preseason", "regular", "season",
    "game", "day", "night", "football", "college", "state", "university",
    "ir", "pup", "nfi", "acl", "mcl", "mri", "espn", "cbs", "nbc", "fox", "pft", "si",
    "new", "los", "las", "san", "green", "tampa", "kansas", "york", "city", "bay", "vegas",
    "england", "orleans", "francisco", "angeles", "jersey", "north", "south", "east", "west",
    "roster", "waivers", "waiver", "injury", "opener", "twitter", "video", "photo", "pick",
    "point", "spread", "note", "notes", "sign", "signed", "cut", "cuts", "this", "that",
    "here", "there", "today", "tomorrow", "yesterday", "just", "also", "after", "before",
    "jr", "sr", "ii", "iii", "iv", "expected", "to", "of", "in", "on", "at", "by", "as", "is",
    "are", "was", "has", "have", "will", "his", "their", "into", "off", "back", "up", "over",
    "out", "news", "moves", "move", "announce", "announces", "adds", "add", "signs", "release",
    "releases", "waive", "waives", "place", "places", "activate", "activates", "elevate",
    "elevates", "promote", "promotes", "claim", "claims", "hard", "knocks", "favorite",
    "if", "i've", "i'm", "we've", "several", "one", "two", "three", "four", "five", "six",
    "another", "familiar", "stunning", "interior", "options", "most", "lose", "make", "latest",
    "ranking", "best", "all", "more", "faces", "pair", "depth", "former", "why", "how", "what",
    "who", "when", "where", "nobody", "everyone", "someone", "recently", "recently-cut",
}
_ABBR_STOP = set(_POS_ABBR.split("|")) | {
    "IR", "PS", "PUP", "NFI", "SUS", "ACL", "MCL", "MRI", "NFL", "AFC", "NFC", "MNF", "SNF", "TNF",
    "ESPN", "CBS", "NBC", "FOX", "GM", "HC", "OC", "DC", "ST", "PFT", "SI", "TV", "US", "UK",
}


def _team_words() -> set[str]:
    words: set[str] = set()
    try:
        for t in get_teams():
            for w in str(t.get("name", "")).split():
                words.add(w.lower())
            words.add(str(t.get("abbr", "")).lower())
    except Exception:  # noqa: BLE001 — config missing in odd test setups
        pass
    words |= {"bucs", "niners", "pats", "bolts", "fins", "jags", "cards", "hawks", "skins", "birds"}
    return words


_TEAM_WORDS: Optional[set[str]] = None


def _team_word_set() -> set[str]:
    global _TEAM_WORDS
    if _TEAM_WORDS is None:
        _TEAM_WORDS = _team_words()
    return _TEAM_WORDS


def _is_stop(token: str) -> bool:
    bare = token.strip(".,'’#")
    if not bare:
        return True
    if bare in _ABBR_STOP:
        return True
    t = bare.lower()
    return t in _GENERIC_STOP or t in _team_word_set()


def _valid_name(m: Optional[re.Match]) -> Optional[str]:
    if m is None:
        return None
    first, last = m.group("first").rstrip(","), m.group("last").rstrip(".,'’")
    if _is_stop(first) or _is_stop(last):
        return None
    parts = [first]
    if m.group("particle"):
        parts.append(m.group("particle"))
    parts.append(last)
    if m.group("suffix"):
        parts.append(m.group("suffix"))
    return " ".join(parts)


def _is_filler(tok: str) -> bool:
    bare = tok.strip(".,")
    if bare in _ABBR_STOP:
        return True
    low = bare.strip("#'’").lower()
    if low in _FILLER_WORDS or low in _team_word_set():
        return True
    return bool(_POSSESSIVE_TEAM_RE.match(tok))


def _names_after(text: str) -> list[str]:
    """Valid names at the start of ``text`` once position / article / team
    fillers are skipped ("RB Ray Davis on IR" -> ["Ray Davis"]). Later
    names in the same run are appended so a roster check can prefer
    "Uso Seumalo" over "Hard Knocks" in "Hard Knocks favorite DT Uso Seumalo"."""
    s = text[:160].lstrip(" ,:-–—")
    while True:
        m = re.match(r"^(#?[\w'’.\-]+)[.,]?\s+", s)
        if not m or not _is_filler(m.group(1)):
            break
        s = s[m.end():]
    first = _valid_name(_NAME_RE.match(s))
    if not first:
        return []
    out = [first]
    for m0 in re.finditer(r"(?<![\w'’])[A-Z]", s):
        if m0.start() == 0:
            continue
        name = _valid_name(_NAME_RE.match(s, m0.start()))
        if name and name not in out:
            out.append(name)
    return out


def _name_after(text: str) -> Optional[str]:
    names = _names_after(text)
    return names[0] if names else None


def _name_before(text: str) -> Optional[str]:
    """Last valid name immediately before a verb, ignoring auxiliaries and
    a trailing parenthetical ("Taylor Rapp (ankle) is signing" -> "Taylor Rapp")."""
    head = _AUX_TAIL_RE.sub("", text[-160:])
    head = re.sub(r"\s*\([^)]*\)\s*$", "", head).rstrip(" ,:;-–—")
    for m in re.finditer(r"(?<![\w'’])[A-Z]", head):
        name = _valid_name(_NAME_END_RE.match(head, m.start()))
        if name:
            return name
    return None


def _verb_span(m: re.Match) -> tuple[int, int]:
    for g in ("v", "v2"):
        try:
            s, e = m.start(g), m.end(g)
        except IndexError:
            continue
        if s >= 0:
            return s, e
    return m.start(), m.end()


# "per Adam Schefter", "via @RapSheet", "sources tell Ian Rapoport" — blanked
# (same length, so match offsets stay valid) before looking for the player.
_ATTRIBUTION_RE = re.compile(
    r"(?:\b(?:per|via|according to|h/t|sources?|source says|reports?|first reported by|"
    r"(?:sources?|as) (?:tell|told|say|said|confirm(?:s|ed)?|first reported)(?: to)?)\s+"
    r"(?:@\w+|[A-Z][\w'’.\-]+(?:\s+[A-Z][\w'’.\-]+)?))|@\w+",
)
_LOOSE_FILLERS = {
    "for", "on", "to", "with", "from", "by", "at", "in", "is", "are", "has", "have", "will",
    "been", "being", "officially", "today", "tonight", "tomorrow", "bring", "brings", "bringing",
    "brought", "elevate", "elevated", "elevates", "elevating", "sign", "signs", "signed", "signing",
    "add", "adds", "added", "adding", "place", "placed", "places", "placing", "activate",
    "activated", "activates", "activating", "promote", "promoted", "promotes", "promoting",
    "put", "puts", "move", "moved", "moves", "moving", "recall", "recalled", "announce",
    "announced", "announces", "list", "window", "21-day",
}


def _name_after_loose(text: str) -> Optional[str]:
    s = text[:160].lstrip(" ,:-–—")
    while True:
        m = re.match(r"^(#?[\w'’.\-]+)[.,]?\s+", s)
        if not m:
            break
        tok = m.group(1)
        if not (_is_filler(tok) or tok.strip(".,").lower() in _LOOSE_FILLERS):
            break
        s = s[m.end():]
    return _valid_name(_NAME_RE.match(s))


def _last_name_before(text: str) -> Optional[str]:
    """Nearest valid name anywhere before the verb ("Jaguars WR Brian Thomas Jr.
    suffers season-ending…" -> "Brian Thomas Jr.")."""
    last = None
    skip_until = -1
    for m0 in re.finditer(r"(?<![\w'’])[A-Z]", text):
        if m0.start() < skip_until:
            continue
        m = _NAME_RE.match(text, m0.start())
        name = _valid_name(m)
        if name:
            last = name
            skip_until = m.end()
    return last


def _name_candidates(text: str, m: re.Match, strict: bool = False) -> list[str]:
    """Player-name candidates for a match, best first: right after the verb
    (team-subject sentences), then right before it (player-subject
    sentences), then — unless ``strict`` — the nearest name before the verb
    / after the whole match ("X suffers season-ending…", "opens 21-day
    window for X")."""
    clean = _ATTRIBUTION_RE.sub(lambda a: " " * len(a.group(0)), text)
    v_start, v_end = _verb_span(m)
    start, end = _clause_bounds(clean, v_start)
    cands: list[str] = []

    def _add(name: Optional[str]) -> None:
        if name and name not in cands:
            cands.append(name)

    after = _names_after(clean[v_end:end])
    _add(after[0] if after else None)
    _add(_name_before(clean[start:v_start]))
    if strict:
        return cands
    for extra in after[1:]:
        _add(extra)
    _add(_last_name_before(clean[start:v_start]))
    _add(_name_after_loose(clean[m.end():end]))
    return cands


def _name_near_verb(text: str, m: re.Match, strict: bool = False) -> Optional[str]:
    cands = _name_candidates(text, m, strict)
    return cands[0] if cands else None


def _name_for_match(
    text: str, m: re.Match, etype: str, known_names: Optional[set[str]] = None,
) -> Optional[str]:
    """Best candidate; when ``known_names`` (roster name keys) is given, the
    first candidate on a roster wins over an earlier unknown one."""
    cands = _name_candidates(text, m, strict=etype in _NAME_REQUIRED_TYPES)
    if not cands:
        return None
    if known_names:
        for c in cands:
            if _name_key(c) in known_names:
                return c
    return cands[0]


def _is_title_case(text: str) -> bool:
    """Headline-style Title Case ("Where Every Cardinals Roster Cut Landed")
    — capitalization no longer marks names, so extraction needs a roster check."""
    words = [w for w in re.findall(r"[A-Za-z][\w'’\-]*", text or "") if len(w) > 3]
    if len(words) < 4:
        return False
    return sum(1 for w in words if w[0].isupper()) / len(words) >= 0.75


def _first_name_anywhere(text: str) -> Optional[str]:
    for m in _NAME_RE.finditer(text or ""):
        name = _valid_name(m)
        if name:
            return name
    return None


def _looks_like_name(text: str) -> bool:
    toks = [t for t in re.split(r"\s+", text.strip()) if t]
    if len(toks) < 2 or len(toks) > 4:
        return False
    return all(re.match(r"^[A-Z][\w'’.\-]*$", t) for t in toks) and not any(_is_stop(t) for t in toks[:2])


def extract_player(item: dict) -> tuple[Optional[str], Optional[str]]:
    """(name, name_key) for a NewsItem dict.

    Priority: structured ``extra.player`` → transaction-title prefix
    ("Keivie Rose: Jaguars (…)") → the capitalized bigram next to the
    classified verb → first plausible name in the title. Requires 2+ tokens.
    """
    extra = item.get("extra") or {}
    name = (extra.get("player") or "").strip()
    if name and len(name.split()) >= 2:
        return name, _name_key(name)
    title = item.get("title") or ""
    is_tx = item.get("source") == NFLCOM_SOURCE or item.get("category") == "transaction"
    if is_tx and ":" in title:
        cand = _extract_player_name(title)
        if _looks_like_name(cand):
            return cand, _name_key(cand)
    found = _match_news_all(title)
    for etype, m in found:
        cand = _name_for_match(title, m, etype)
        if cand:
            return cand, _name_key(cand)
    cand = _first_name_anywhere(title)
    if cand:
        return cand, _name_key(cand)
    return None, None


# ---------------------------------------------------------------------------
# Team helpers
# ---------------------------------------------------------------------------

_ALIAS_TO_NEWS = {
    "bucs": "TB", "niners": "SF", "pats": "NE", "bolts": "LAC", "fins": "MIA", "jags": "JAX",
    "cards": "ARI", "hawks": "SEA", "skins": "WAS", "nyg": "NYG", "nyj": "NYJ",
}


def _teams_in_text(text: str) -> list[str]:
    """News-style abbreviations for nicknames / hashtags mentioned in ``text``."""
    idx = _nickname_index()
    out: list[str] = []
    for tok in re.findall(r"#?[A-Za-z0-9][A-Za-z0-9'’]+", text or ""):
        key = re.sub(r"['’]s?$", "", tok.lstrip("#")).lower()
        abbr = idx.get(key) or _ALIAS_TO_NEWS.get(key)
        if abbr and abbr not in out:
            out.append(abbr)
    return out


def _team_for_match(item: dict, title: str, m: Optional[re.Match]) -> str:
    teams = [t for t in (item.get("teams") or []) if t]
    if len(teams) == 1:
        return teams[0]
    if m is not None:
        start, end = _clause_bounds(title, m.start())
        clause_teams = _teams_in_text(title[start:end])
        for t in clause_teams:
            if not teams or t in teams:
                return t
    if teams:
        return teams[0]
    found = _teams_in_text(title)
    return found[0] if found else ""


# ---------------------------------------------------------------------------
# Event construction
# ---------------------------------------------------------------------------

_JOIN_EVENTS = {"claimed", "signed", "ps_signed", "ps_promoted", "traded", "team_change", "ps_elevated"}
_LEAVE_EVENTS = {"waived", "released", "ps_released", "injury_settlement", "retired"}

_SCHEDULE_CACHE: Optional[list[dict]] = None


def _schedule() -> list[dict]:
    global _SCHEDULE_CACHE
    if _SCHEDULE_CACHE is None:
        try:
            _SCHEDULE_CACHE = load_schedule()
        except Exception:  # noqa: BLE001
            _SCHEDULE_CACHE = []
    return _SCHEDULE_CACHE


def _week_for(date_str: str, schedule: Optional[list[dict]] = None) -> Optional[int]:
    sched = schedule if schedule is not None else _schedule()
    if not sched:
        return None
    try:
        return week_from_date(sched, date_str)
    except Exception:  # noqa: BLE001
        return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _event_id(source: str, date_str: str, name_key: str, event_type: str, team: str) -> str:
    raw = f"{source}|{date_str}|{name_key}|{event_type}|{team}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def make_event(
    *,
    date_str: str,
    name: str,
    event_type: str,
    source: str,
    source_kind: str,
    confidence: str,
    team: str = "",
    from_team: str = "",
    to_team: str = "",
    pos: str = "",
    detail: str = "",
    url: str = "",
    gsis_id: Optional[str] = None,
    name_key: Optional[str] = None,
    season: Optional[int] = None,
    schedule: Optional[list[dict]] = None,
) -> dict:
    """Build one normalized event record (see module docstring)."""
    if event_type not in EVENT_TYPES:
        logger.warning("Unknown event_type %r -> status_change", event_type)
        event_type = "status_change"
    team = (team or to_team or from_team or "").upper()
    if event_type in _JOIN_EVENTS and not to_team:
        to_team = team
    if event_type in _LEAVE_EVENTS and not from_team:
        from_team = team
    nk = name_key if name_key is not None else _name_key(name or "")
    return {
        "event_id": _event_id(source, date_str, nk, event_type, team),
        "date": date_str,
        "season": season if season is not None else get_season_year(),
        "week": _week_for(date_str, schedule),
        "observed_at": _now_iso(),
        "gsis_id": gsis_id or None,
        "name": name or "",
        "name_key": nk,
        "team": team,
        "from_team": (from_team or "").upper(),
        "to_team": (to_team or "").upper(),
        "pos": (pos or "").upper(),
        "event_type": event_type,
        "detail": (detail or "")[:200],
        "source": source,
        "source_kind": source_kind,
        "confidence": confidence,
        "url": url or "",
        "confirmed_by": None,
    }


def _item_date(item: dict, fallback: str) -> str:
    pub = str(item.get("published") or "")[:10]
    try:
        date.fromisoformat(pub)
        return pub
    except ValueError:
        return fallback


def _is_nflcom_tx(item: dict) -> bool:
    extra = item.get("extra") or {}
    if extra.get("kind") == "nfl_transaction":
        return True
    return item.get("source") == NFLCOM_SOURCE and item.get("category") == "transaction"


def _tx_type_from_title(title: str) -> str:
    m = re.search(r"\(([^()]*)\)\s*$", title or "")
    if m:
        return m.group(1).strip()
    parts = (title or "").split(":", 1)
    return parts[1].strip() if len(parts) == 2 else ""


def normalize_nflcom(items: list[dict], date_str: str, schedule: Optional[list[dict]] = None) -> list[dict]:
    """NFL.com transaction NewsItem dicts -> official events.

    Uses the structured ``extra`` block when the collector produced one;
    older raw files fall back to the title ("Name: Team (Description)").
    """
    out: list[dict] = []
    for item in items:
        if not _is_nflcom_tx(item):
            continue
        extra = item.get("extra") or {}
        title = item.get("title") or ""
        tx_type = (extra.get("tx_type") or "").strip() or _tx_type_from_title(title)
        etype = classify_nflcom(tx_type)
        name, name_key = extract_player(item)
        if not name:
            logger.debug("nflcom: no player name in %r", title)
            continue
        teams = [t for t in (item.get("teams") or []) if t]
        from_team = (extra.get("from_team") or "").upper()
        to_team = (extra.get("to_team") or "").upper()
        if not from_team and not to_team:
            if "->" in title and len(teams) >= 2:
                from_team, to_team = teams[0], teams[-1]
            elif teams:
                if etype in _LEAVE_EVENTS:
                    from_team = teams[0]
                elif etype in _JOIN_EVENTS:
                    to_team = teams[0]
        team = to_team if etype in _JOIN_EVENTS else (from_team or to_team)
        if not team:
            team = teams[0] if teams else ""
        ev_date = (extra.get("tx_date") or "").strip() or _item_date(item, date_str)
        out.append(make_event(
            date_str=ev_date,
            name=name,
            name_key=name_key,
            event_type=etype,
            source="nflcom_transactions",
            source_kind="official",
            confidence="official",
            team=team,
            from_team=from_team,
            to_team=to_team,
            pos=(extra.get("position") or ""),
            detail=tx_type,
            url=item.get("url") or "",
            schedule=schedule,
        ))
    return out


_NEWS_SKIP_SOURCE_TYPES = {"youtube", "podcast"}


def normalize_news(
    items: list[dict],
    date_str: str,
    schedule: Optional[list[dict]] = None,
    known_names: Optional[set[str]] = None,
) -> list[dict]:
    """News / tweet / injury / non-NFL.com transaction items -> reported events.

    Precision first: an event is only emitted when a pattern fires *and* a
    player name sits next to the verb *and* a team can be attributed (from
    ``item.teams`` or a nickname/hashtag in the clause). Title-Case
    headlines additionally need the name to exist in ``known_names``
    (nflverse name keys) when that set is supplied.
    """
    out: list[dict] = []
    for item in items:
        if _is_nflcom_tx(item) or item.get("source") == NFLCOM_SOURCE:
            continue
        if item.get("source_type") in _NEWS_SKIP_SOURCE_TYPES:
            continue
        title = item.get("title") or ""
        matches = _match_news_all(title)
        if not matches:
            continue
        headline = _is_title_case(title)
        seen: set[tuple[str, str]] = set()
        for etype, m in matches:
            name = _name_for_match(title, m, etype, known_names)
            if not name:
                continue
            name_key = _name_key(name)
            if headline and known_names is not None and name_key not in known_names:
                logger.debug("news: headline name %r not on any roster, skipped (%s)", name, title[:80])
                continue
            if (etype, name_key) in seen:
                continue
            team = _team_for_match(item, title, m)
            if not team:
                logger.debug("news: no team for %r (%s)", title[:80], etype)
                continue
            seen.add((etype, name_key))
            start, end = _clause_bounds(title, m.start())
            out.append(make_event(
                date_str=_item_date(item, date_str),
                name=name,
                name_key=name_key,
                event_type=etype,
                source=f"news:{item.get('source') or item.get('source_type') or 'unknown'}",
                source_kind="news",
                confidence="reported",
                team=team,
                detail=title[start:end].strip()[:160],
                url=item.get("url") or "",
                schedule=schedule,
            ))
    return out


_OURLADS_PLACED = {"IR": "ir_placed", "PUP": "pup_placed", "NFI": "nfi_placed", "SUS": "suspended"}
_OURLADS_ACTIVATED = {"IR": "ir_activated", "PUP": "pup_activated", "NFI": "nfi_activated", "SUS": "reinstated"}


def normalize_ourlads(status_changes: list[dict], date_str: str, schedule: Optional[list[dict]] = None) -> list[dict]:
    """``split_reserve_changes`` status records -> confirmed OurLads events.

    Active -> bucket / bucket -> Active are dated placements/activations.
    A player *first seen* in a bucket (``old_status`` None) or dropped from
    one (``new_status`` None) only yields ``status_change``: OurLads can't
    say when the move happened (often it's a name-format change), so it
    must not set ``ir_date``.
    """
    out: list[dict] = []
    for c in status_changes or []:
        if c.get("type") not in (None, "status_change"):
            continue
        old_s = (c.get("old_status") or "").upper() or None
        new_s = (c.get("new_status") or "").upper() or None
        old_b = old_s if old_s in _OURLADS_PLACED else None
        new_b = new_s if new_s in _OURLADS_PLACED else None
        if new_b and old_s is not None:
            etype = _OURLADS_PLACED[new_b]
        elif old_b and new_s == "ACTIVE":
            etype = _OURLADS_ACTIVATED[old_b]
        else:
            etype = "status_change"
        name = c.get("name") or ""
        if not name:
            continue
        out.append(make_event(
            date_str=date_str,
            name=name,
            event_type=etype,
            source="ourlads",
            source_kind="ourlads",
            confidence="confirmed",
            team=to_news(c.get("team") or "", "ourlads"),
            pos=(c.get("generic_pos") or c.get("pos") or ""),
            detail=c.get("message") or f"{old_s or '-'} -> {new_s or '-'}",
            schedule=schedule,
        ))
    return out


_NFLVERSE_PLACED = {"IR": "ir_placed", "PUP": "pup_placed", "NFI": "nfi_placed", "SUS": "suspended",
                    "EXE": "exempt", "RET": "retired"}
_NFLVERSE_ACTIVATED = {"IR": "ir_activated", "PUP": "pup_activated", "NFI": "nfi_activated",
                       "SUS": "reinstated", "EXE": "reinstated"}
_ELEVATION_ROUNDTRIP_DAYS = 7


def _label(status: Optional[str], abbr: Optional[str], given: Optional[str]) -> Optional[str]:
    if given:
        return given
    if not status:
        return None
    return status_label(status, abbr or "")


def normalize_nflverse(
    transitions: list[dict],
    date_str: str,
    existing_events: Optional[list[dict]] = None,
    schedule: Optional[list[dict]] = None,
) -> list[dict]:
    """nflverse day-over-day transitions -> events.

    ``DEV -> ACT`` is emitted as ``ps_promoted`` with confidence *reported*
    (a standard elevation looks identical in the file until it reverts).
    When the reverse ``ACT -> DEV`` flip arrives within 7 days, the earlier
    ``ps_promoted`` in ``existing_events`` is relabeled ``ps_elevated`` in
    place (the caller persists it via :func:`save_events`) and no
    ``ps_signed`` is emitted for the reversion.
    """
    out: list[dict] = []
    existing = existing_events if existing_events is not None else []
    for t in transitions or []:
        kind = t.get("kind")
        if kind == "removed":
            continue
        name = t.get("name") or ""
        if not name:
            continue
        old_lab = _label(t.get("old_status"), t.get("old_abbr"), t.get("old_label"))
        new_lab = _label(t.get("new_status"), t.get("new_abbr"), t.get("new_label"))
        old_team = to_news(t.get("old_team") or "", "nflverse")
        new_team = to_news(t.get("new_team") or "", "nflverse")
        team_changed = bool(old_team and new_team and old_team != new_team)
        detail = f"nflverse {t.get('old_status') or '-'}/{t.get('old_abbr') or '-'} -> " \
                 f"{t.get('new_status') or '-'}/{t.get('new_abbr') or '-'}"
        common = dict(
            date_str=date_str, name=name, name_key=t.get("name_key") or None,
            gsis_id=t.get("gsis_id"), pos=t.get("pos") or "", detail=detail,
            source="nflverse", source_kind="nflverse", schedule=schedule,
        )

        if kind == "added":
            old_lab = "FA"
        if old_lab == new_lab and not team_changed:
            logger.debug("nflverse: abbr-only change ignored for %s (%s)", name, detail)
            continue

        etype: Optional[str] = None
        confidence = "confirmed"
        if new_lab in _NFLVERSE_PLACED and new_lab != old_lab:
            etype = _NFLVERSE_PLACED[new_lab]
        elif new_lab == "ACT" and old_lab in _NFLVERSE_ACTIVATED:
            etype = _NFLVERSE_ACTIVATED[old_lab]
        elif new_lab == "ACT" and old_lab == "PS":
            etype, confidence = "ps_promoted", "reported"
        elif new_lab == "ACT" and old_lab in ("FA", None, "UNKNOWN"):
            etype = "signed"
        elif new_lab == "PS" and old_lab == "ACT":
            if _relabel_elevation(existing, t, date_str):
                continue
            etype = "ps_signed"
        elif new_lab == "PS":
            etype = "ps_signed"
        elif new_lab == "FA":
            etype = "released"
        elif team_changed:
            etype = "team_change"
        else:
            etype = "status_change"

        if team_changed:
            common.update(from_team=old_team, to_team=new_team, team=new_team)
        else:
            common.update(team=new_team or old_team)
        out.append(make_event(event_type=etype, confidence=confidence, **common))
    return out


def _relabel_elevation(existing: list[dict], t: dict, date_str: str) -> bool:
    """ACT -> DEV within 7 days of an nflverse ``ps_promoted`` for the same
    player = it was a standard elevation. Relabel in place; True when done."""
    try:
        today = date.fromisoformat(date_str)
    except ValueError:
        return False
    gsis, nk = t.get("gsis_id"), t.get("name_key")
    for ev in reversed(existing):
        if ev.get("source_kind") != "nflverse" or ev.get("event_type") != "ps_promoted":
            continue
        same = (gsis and ev.get("gsis_id") == gsis) or (nk and ev.get("name_key") == nk)
        if not same:
            continue
        try:
            delta = (today - date.fromisoformat(ev["date"])).days
        except (KeyError, ValueError):
            continue
        if 0 <= delta <= _ELEVATION_ROUNDTRIP_DAYS:
            ev["event_type"] = "ps_elevated"
            ev["confidence"] = "confirmed"
            ev["detail"] = (ev.get("detail") or "") + f"; reverted to PS {date_str} (standard elevation)"
            ev["relabeled_from"] = "ps_promoted"
            return True
        break
    return False


# ---------------------------------------------------------------------------
# Ledger persistence
# ---------------------------------------------------------------------------


def _base_dir() -> Path:
    """``data/roster`` — monkeypatched by tests to a tmp dir."""
    return get_data_dir("roster")


def _events_path() -> Path:
    return _base_dir() / "events.jsonl"


def _state_path() -> Path:
    return _base_dir() / "state.json"


def load_events() -> list[dict]:
    path = _events_path()
    if not path.exists():
        return []
    events: list[dict] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                logger.warning("Skipping bad ledger line: %s", line[:80])
    return events


def save_events(events: list[dict]) -> Path:
    """Rewrite the whole ledger (used after relabels / confirmations)."""
    path = _events_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
    return path


def _near(a: str, b: str, days: int = 1) -> bool:
    try:
        return abs((date.fromisoformat(a) - date.fromisoformat(b)).days) <= days
    except ValueError:
        return a == b


def merge_new_events(existing: list[dict], new: list[dict]) -> list[dict]:
    """Events from ``new`` that are neither already in ``existing`` (by id)
    nor near-duplicates (same name_key + event_type + source_kind within
    ±1 day) of an existing or earlier-new event."""
    ids = {e.get("event_id") for e in existing}
    by_key: dict[tuple[str, str, str], list[str]] = {}
    for e in existing:
        by_key.setdefault((e.get("name_key", ""), e.get("event_type", ""), e.get("source_kind", "")), []).append(e.get("date", ""))
    accepted: list[dict] = []
    for ev in new:
        eid = ev.get("event_id")
        if eid in ids:
            continue
        key = (ev.get("name_key", ""), ev.get("event_type", ""), ev.get("source_kind", ""))
        if any(_near(ev.get("date", ""), d) for d in by_key.get(key, [])):
            continue
        ids.add(eid)
        by_key.setdefault(key, []).append(ev.get("date", ""))
        accepted.append(ev)
    return accepted


def append_events(events: list[dict]) -> int:
    """Append deduplicated events to the ledger; returns the count appended."""
    existing = load_events()
    accepted = merge_new_events(existing, events)
    if not accepted:
        return 0
    path = _events_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        for ev in accepted:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
    logger.info("Roster ledger: appended %d events (%d submitted)", len(accepted), len(events))
    return len(accepted)


# ---------------------------------------------------------------------------
# Confirmation
# ---------------------------------------------------------------------------

_EVENT_FAMILY = {
    "ir_placed": "ir_in", "ir_activated": "ir_out", "ir_designated_return": "ir_designated",
    "pup_placed": "pup_in", "pup_activated": "pup_out",
    "nfi_placed": "nfi_in", "nfi_activated": "nfi_out",
    "suspended": "suspended", "reinstated": "reinstated", "exempt": "exempt",
    "ps_signed": "ps_in", "ps_released": "ps_out", "ps_elevated": "elevated", "ps_promoted": "join",
    "waived": "cut", "released": "cut", "injury_settlement": "cut",
    "claimed": "join", "signed": "join", "traded": "join", "team_change": "join",
    "retired": "retired", "status_change": "other",
}


def _family(etype: str) -> str:
    return _EVENT_FAMILY.get(etype, etype)


def _is_confirming(ev: dict) -> bool:
    kind = ev.get("source_kind")
    if kind in ("official", "ourlads"):
        return True
    return kind == "nflverse" and ev.get("confidence") != "reported"


def confirm_reported(events: list[dict], window_days: int = 3) -> list[dict]:
    """Link reported events to an official/nflverse/OurLads event for the
    same player + event family within ``window_days``; sets ``confirmed_by``
    and upgrades ``confidence`` to *confirmed*. Mutates and returns ``events``."""
    confirming: dict[tuple[str, str], list[dict]] = {}
    for ev in events:
        if _is_confirming(ev):
            confirming.setdefault((ev.get("name_key", ""), _family(ev.get("event_type", ""))), []).append(ev)
    n = 0
    for ev in events:
        if ev.get("confidence") != "reported" or ev.get("confirmed_by"):
            continue
        cands = confirming.get((ev.get("name_key", ""), _family(ev.get("event_type", ""))), [])
        for c in cands:
            if c.get("event_id") == ev.get("event_id"):
                continue
            if _near(ev.get("date", ""), c.get("date", ""), window_days):
                ev["confirmed_by"] = c["event_id"]
                ev["confidence"] = "confirmed"
                n += 1
                break
    if n:
        logger.info("Roster ledger: %d reported events confirmed", n)
    return events


# ---------------------------------------------------------------------------
# GSIS resolution
# ---------------------------------------------------------------------------

_NAME_INDEX_CACHE: dict[int, tuple[int, dict[str, list[str]]]] = {}


def _name_index(players: dict[str, dict]) -> dict[str, list[str]]:
    key = id(players)
    cached = _NAME_INDEX_CACHE.get(key)
    if cached and cached[0] == len(players):
        return cached[1]
    idx: dict[str, list[str]] = {}
    for gsis, p in players.items():
        nk = p.get("name_key") or _name_key(p.get("name") or "")
        if nk:
            idx.setdefault(nk, []).append(gsis)
    if len(_NAME_INDEX_CACHE) > 8:
        _NAME_INDEX_CACHE.clear()
    _NAME_INDEX_CACHE[key] = (len(players), idx)
    return idx


def resolve_gsis(
    name_key: str,
    team: str,
    nflverse_players: dict[str, dict],
    weekly_players: Optional[dict[str, dict]] = None,
) -> Optional[str]:
    """GSIS id for ``name_key``: unique name match, else the candidate on
    ``team`` (news abbr), else ``weekly_players`` (sheet rows keyed by GSIS),
    else None — never a guess between two same-named players."""
    if not name_key:
        return None
    for players in (nflverse_players or {}, weekly_players or {}):
        if not players:
            continue
        cands = _name_index(players).get(name_key, [])
        if len(cands) == 1:
            return cands[0]
        if len(cands) > 1 and team:
            same = [g for g in cands if to_news(players[g].get("team") or "") == to_news(team)]
            if len(same) == 1:
                return same[0]
    return None


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

_STATUS_EFFECT: dict[str, Optional[str]] = {
    "ir_placed": "IR", "pup_placed": "PUP", "nfi_placed": "NFI", "suspended": "SUS",
    "exempt": "EXE", "retired": "RET",
    "ir_activated": "ACT", "pup_activated": "ACT", "nfi_activated": "ACT", "reinstated": "ACT",
    "ps_signed": "PS", "ps_released": "FA", "ps_promoted": "ACT",
    "waived": "FA", "released": "FA", "injury_settlement": "FA",
    "claimed": "ACT", "signed": "ACT",
    "ir_designated_return": None, "ps_elevated": None, "traded": None,
    "team_change": None, "status_change": None,
}
_RESERVE_PLACEMENTS = {"ir_placed": "IR", "pup_placed": "PUP", "nfi_placed": "NFI"}
_OFFICIAL_GRACE_DAYS = 1


def _new_player(key: str, gsis: Optional[str], name: str, name_key: str, team: str, pos: str) -> dict:
    return {
        "gsis_id": gsis, "name": name, "name_key": name_key, "team": team, "pos": pos,
        "status": "UNKNOWN", "status_abbr": "", "status_source": None, "status_since": None,
        "ir_date": None, "designated_return_date": None, "earliest_return_week": None,
        "elevations_used": 0, "elevations_reported": 0, "elevation_dates": [],
        "last_event": None, "pending": [],
        "_key": key,
    }


def _sort_key(ev: dict) -> tuple:
    return (ev.get("date") or "", _CONFIDENCE_RANK.get(ev.get("confidence"), 0), ev.get("observed_at") or "")


def build_state(
    events: list[dict],
    nflverse_players: dict[str, dict],
    schedule: list[dict],
    settings: Optional[dict] = None,
    baseline_date: Optional[str] = None,
    as_of: Optional[str] = None,
) -> dict:
    """Replay the ledger over the nflverse baseline.

    * Baseline: every nflverse player gets its resolved status.
    * Events are applied in ``(date, confidence, observed_at)`` order so on
      a given date official beats confirmed beats reported.
    * Events dated before the baseline snapshot are *history only*: they
      fill ``ir_date`` / ``earliest_return_week`` when the baseline already
      shows the player on that list, and always feed the elevation counters
      and ``last_event``. Official events get a one-day grace (nflverse lags
      NFL.com by up to a day).
    * Reported events change status only when ``roster.apply_reported_events``
      is on; unconfirmed ones are listed under the player's ``pending``.
    """
    settings = settings if settings is not None else get_settings()
    roster_cfg = settings.get("roster", {}) if isinstance(settings, dict) else {}
    min_games = int(roster_cfg.get("ir_min_games", 4))
    apply_reported = bool(roster_cfg.get("apply_reported_events", True))
    season = get_season_year(settings)
    as_of = as_of or date.today().isoformat()
    baseline_date = baseline_date or as_of
    players_src = nflverse_players or {}

    players: dict[str, dict] = {}
    for gsis, p in players_src.items():
        rec = _new_player(gsis, gsis, p.get("name", ""), p.get("name_key") or _name_key(p.get("name", "")),
                          to_news(p.get("team") or ""), p.get("pos", ""))
        rec["status"] = p.get("label") or status_label(p.get("status", ""), p.get("status_abbr", ""))
        rec["status_abbr"] = p.get("status_abbr", "")
        rec["status_source"] = "nflverse"
        rec["status_since"] = baseline_date
        players[gsis] = rec

    def _player_for(ev: dict) -> dict:
        gsis = ev.get("gsis_id") or resolve_gsis(ev.get("name_key", ""), ev.get("team", ""), players_src)
        if gsis and gsis in players:
            return players[gsis]
        key = f"name:{ev.get('name_key', '')}"
        if key not in players:
            players[key] = _new_player(key, None, ev.get("name", ""), ev.get("name_key", ""),
                                       ev.get("team", ""), ev.get("pos", ""))
        return players[key]

    def _return_week(team: str, placed: str) -> Optional[int]:
        if not schedule or not team:
            return None
        try:
            return earliest_return_week(schedule, to_proj(team), placed, min_games=min_games)
        except Exception:  # noqa: BLE001
            return None

    for ev in sorted(events, key=_sort_key):
        etype = ev.get("event_type", "status_change")
        ev_date = ev.get("date") or as_of
        conf = ev.get("confidence", "reported")
        rec = _player_for(ev)
        if not rec.get("pos") and ev.get("pos"):
            rec["pos"] = ev["pos"]

        # Events older than the nflverse baseline are history for players the
        # baseline covers; a name-only player has no baseline to protect.
        grace = _OFFICIAL_GRACE_DAYS if ev.get("source_kind") == "official" else 0
        try:
            history_only = (rec.get("gsis_id") is not None
                            and date.fromisoformat(ev_date) + timedelta(days=grace) < date.fromisoformat(baseline_date))
        except ValueError:
            history_only = False
        reported_unconfirmed = conf == "reported" and not ev.get("confirmed_by")
        can_apply = not history_only and (apply_reported or not reported_unconfirmed)

        target = _STATUS_EFFECT.get(etype)
        noop = target is not None and target == rec["status"] and not history_only

        # --- elevation counters (season-long, independent of baseline) ---
        if etype == "ps_elevated":
            if not any(_near(ev_date, d) for d in rec["elevation_dates"]):
                rec["elevation_dates"].append(ev_date)
                if reported_unconfirmed:
                    rec["elevations_reported"] += 1
                else:
                    rec["elevations_used"] += 1

        # --- reserve-list dates (also fill from history when baseline agrees) ---
        if etype in _RESERVE_PLACEMENTS:
            lst = _RESERVE_PLACEMENTS[etype]
            applies = can_apply or (history_only and rec["status"] == lst)
            if applies and not (rec["status"] == lst and rec["ir_date"] and _near(ev_date, rec["ir_date"], 3)):
                rec["ir_date"] = ev_date
                rec["earliest_return_week"] = _return_week(ev.get("team") or rec["team"], ev_date)
                rec["designated_return_date"] = None
        elif etype == "ir_designated_return" and (can_apply or history_only and rec["status"] in ("IR", "PUP", "NFI")):
            rec["designated_return_date"] = ev_date

        # --- status + team ---
        if can_apply:
            if target is not None:
                rec["status"] = target
                rec["status_abbr"] = ""
                rec["status_source"] = ev.get("source_kind")
                rec["status_since"] = ev_date
                if target == "ACT" and etype in ("ir_activated", "pup_activated", "nfi_activated"):
                    rec["ir_date"] = None
                    rec["earliest_return_week"] = None
                    rec["designated_return_date"] = None
            if etype in _JOIN_EVENTS and (ev.get("to_team") or ev.get("team")):
                rec["team"] = ev.get("to_team") or ev.get("team")
            elif etype not in _LEAVE_EVENTS and ev.get("team") and not rec.get("team"):
                rec["team"] = ev["team"]
            if reported_unconfirmed and not noop:
                rec["pending"].append({
                    "event_id": ev.get("event_id"), "event_type": etype,
                    "date": ev_date, "source": ev.get("source"),
                })
        elif not history_only and reported_unconfirmed and not noop:
            rec["pending"].append({
                "event_id": ev.get("event_id"), "event_type": etype,
                "date": ev_date, "source": ev.get("source"), "applied": False,
            })

        rec["last_event"] = {"event_id": ev.get("event_id"), "event_type": etype, "date": ev_date,
                             "confidence": conf, "source": ev.get("source")}

    by_name: dict[str, str] = {}
    ambiguous: set[str] = set()
    for key, rec in players.items():
        nk = rec.get("name_key")
        if not nk:
            continue
        if nk in by_name and by_name[nk] != key:
            ambiguous.add(nk)
            continue
        by_name[nk] = key
    for rec in players.values():
        rec.pop("_key", None)

    return {
        "season": season,
        "updated_at": _now_iso(),
        "as_of": as_of,
        "as_of_week": _week_for(as_of, schedule),
        "baseline": {"source": "nflverse", "date": baseline_date, "players": len(players_src)},
        "players": players,
        "by_name": by_name,
        "ambiguous_names": sorted(ambiguous),
    }


def load_state() -> Optional[dict]:
    path = _state_path()
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("Unreadable roster state %s: %s", path, e)
        return None


def save_state(state: dict) -> Path:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=1, ensure_ascii=False), encoding="utf-8")
    logger.info("Saved roster state: %d players -> %s", len(state.get("players", {})), path)
    return path


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _status_counts(state: dict) -> dict[str, int]:
    counts: dict[str, int] = {}
    for rec in state.get("players", {}).values():
        counts[rec.get("status", "UNKNOWN")] = counts.get(rec.get("status", "UNKNOWN"), 0) + 1
    return dict(sorted(counts.items()))


def run_roster_step(
    date_str: str,
    news_items: Optional[list[dict]] = None,
    dc_status_changes: Optional[list[dict]] = None,
    nflverse_players: Optional[dict[str, dict]] = None,
    prev_nflverse: Optional[dict[str, dict]] = None,
    settings: Optional[dict] = None,
    schedule: Optional[list[dict]] = None,
) -> dict:
    """One pipeline step: nflverse diff → NFL.com → news → OurLads events,
    append to the ledger, confirm reported events, rebuild + save state.

    Returns ``{"new_events": [...], "state": state, "counts": {...}}``;
    ``new_events`` are enriched with ``earliest_return_week`` /
    ``elevations_used`` from the rebuilt state so the report can print
    "eligible Wk N" and "elevation n/3".
    """
    settings = settings if settings is not None else get_settings()
    roster_cfg = settings.get("roster", {})
    if schedule is None:
        schedule = _schedule()

    # Explicit nflverse args mean the caller controls the diff (run_daily
    # passes prev=None on purpose when today's fetch failed); only when
    # nothing is given do we fall back to the two newest snapshots on disk.
    nfl_date: Optional[str] = None
    if nflverse_players is None:
        nflverse_players, nfl_date = latest_nflverse_snapshot()
        if nflverse_players is None:
            logger.warning("No nflverse snapshot on disk — state will only contain event-derived players")
            nflverse_players = {}
        elif prev_nflverse is None:
            prev_nflverse, _prev_date = latest_nflverse_snapshot(before_date=nfl_date)
    else:
        nfl_date = date_str

    existing = load_events()
    new: list[dict] = []
    counts: dict[str, int] = {}

    if nflverse_players and prev_nflverse:
        transitions = diff_nflverse(nflverse_players, prev_nflverse)
        nv = normalize_nflverse(transitions, date_str, existing_events=existing, schedule=schedule)
        counts["nflverse_transitions"] = len(transitions)
        counts["nflverse_events"] = len(nv)
        new.extend(nv)
    if news_items:
        tx = normalize_nflcom(news_items, date_str, schedule=schedule)
        known = {p.get("name_key") for p in nflverse_players.values() if p.get("name_key")} if nflverse_players else None
        nw = normalize_news(news_items, date_str, schedule=schedule, known_names=known)
        counts["nflcom_events"] = len(tx)
        counts["news_events"] = len(nw)
        new.extend(tx)
        new.extend(nw)
    if dc_status_changes:
        ol = normalize_ourlads(dc_status_changes, date_str, schedule=schedule)
        counts["ourlads_events"] = len(ol)
        new.extend(ol)

    accepted = merge_new_events(existing, new)
    events = existing + accepted
    events = confirm_reported(events, window_days=int(roster_cfg.get("confirm_window_days", 3)))
    counts["appended"] = len(accepted)
    counts["ledger"] = len(events)
    counts["confirmed_new"] = sum(1 for e in accepted if e.get("confirmed_by"))

    state = build_state(events, nflverse_players, schedule, settings=settings,
                        baseline_date=nfl_date, as_of=date_str)
    save_state(state)
    counts["status"] = _status_counts(state)

    # Enrich the new events from the rebuilt state: NFL.com's table carries
    # no position or id, so gsis_id / pos come from the nflverse baseline
    # (OurLads as a last resort), plus the derived IR / elevation fields.
    by_name = state.get("by_name", {})
    for ev in accepted:
        key = ev.get("gsis_id") or by_name.get(ev.get("name_key", ""))
        rec = state["players"].get(key) if key else None
        if rec:
            if not ev.get("gsis_id") and rec.get("gsis_id"):
                ev["gsis_id"] = rec["gsis_id"]
            if not ev.get("pos") and rec.get("pos"):
                ev["pos"] = rec["pos"]
            if ev["event_type"] in _RESERVE_PLACEMENTS and rec.get("earliest_return_week"):
                ev["earliest_return_week"] = rec["earliest_return_week"]
            if ev["event_type"] == "ps_elevated":
                ev["elevations_used"] = rec.get("elevations_used", 0)
                ev["max_elevations"] = int(roster_cfg.get("max_elevations", 3))
        if not ev.get("pos"):
            ev["pos"] = _ourlads_pos(ev.get("name", ""))
    save_events(events)

    logger.info("Roster step %s: %s", date_str, json.dumps({k: v for k, v in counts.items() if k != "status"}))
    return {"new_events": accepted, "state": state, "counts": counts}


def _ourlads_pos(name: str) -> str:
    """Generic position from the latest OurLads depth chart, or ""."""
    if not name:
        return ""
    try:
        from collectors.depth_chart_collector import RESERVE_BUCKETS, lookup_player
        rec = lookup_player(name) or {}
        pos = str(rec.get("generic_pos") or rec.get("pos") or "").upper()
        return "" if pos in RESERVE_BUCKETS else pos
    except Exception:  # noqa: BLE001 — enrichment only
        return ""


# ---------------------------------------------------------------------------
# Backfill helper + CLI
# ---------------------------------------------------------------------------


def _load_raw_items(date_str: str, files: Iterable[str] = ("web.json",)) -> list[dict]:
    items: list[dict] = []
    raw_dir = PROJECT_ROOT / "data" / "raw" / date_str
    for fname in files:
        path = raw_dir / fname
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, list):
            items.extend(i for i in payload if isinstance(i, dict))
    return items


def backfill_nflcom_events(start: str, end: Optional[str] = None) -> int:
    """Walk ``data/raw/<date>/web.json`` from ``start`` to ``end`` and append
    official NFL.com transaction events (old-style titles are fine)."""
    d = date.fromisoformat(start)
    stop = date.fromisoformat(end) if end else date.today()
    total = 0
    while d <= stop:
        ds = d.isoformat()
        items = _load_raw_items(ds)
        if items:
            evs = normalize_nflcom(items, ds)
            n = append_events(evs)
            total += n
            logger.info("backfill %s: %d transaction events (%d new)", ds, len(evs), n)
        d += timedelta(days=1)
    return total


def main(argv: Optional[list[str]] = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Roster events ledger / state tools")
    ap.add_argument("--rebuild", action="store_true", help="replay events.jsonl over the latest nflverse snapshot")
    ap.add_argument("--date", default=None, help="as-of date for --rebuild (default today)")
    ap.add_argument("--classify", metavar="TITLE", help="print the news classification for a title")
    ap.add_argument("--backfill", metavar="START", help="append NFL.com events from data/raw/<date>/web.json since START")
    ap.add_argument("--end", default=None, help="end date for --backfill (default today)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    if args.classify:
        title = args.classify
        found = _match_news_all(title)
        item = {"title": title, "teams": []}
        print(f"title: {title}")
        if not found:
            print("classification: None")
        for etype, m in found:
            print(f"classification: {etype}  verb={title[_verb_span(m)[0]:_verb_span(m)[1]]!r}"
                  f"  player={_name_for_match(title, m, etype)!r}  team={_team_for_match(item, title, m)!r}")
        return 0

    if args.backfill:
        n = backfill_nflcom_events(args.backfill, args.end)
        print(f"Backfill appended {n} events")

    if args.rebuild or args.backfill:
        as_of = args.date or date.today().isoformat()
        players, nfl_date = latest_nflverse_snapshot()
        if players is None:
            print("No nflverse snapshot found — run collectors/nflverse_roster_collector.py first")
            return 1
        events = load_events()
        settings = get_settings()
        events = confirm_reported(events, window_days=int(settings.get("roster", {}).get("confirm_window_days", 3)))
        save_events(events)
        schedule = load_schedule(settings=settings)
        state = build_state(events, players, schedule, settings=settings, baseline_date=nfl_date, as_of=as_of)
        path = save_state(state)
        print(f"State rebuilt from {len(events)} events over nflverse {nfl_date} -> {path}")
        print("Status counts:", json.dumps(_status_counts(state)))
        ir = [p for p in state["players"].values() if p["status"] == "IR" and p.get("earliest_return_week")]
        ir.sort(key=lambda p: (p.get("ir_date") or "", p["name"]))
        print(f"IR players with a return week: {len(ir)}")
        for p in ir[:10]:
            print(f"  {p['name']} ({p['team']} {p['pos']}) IR since {p['ir_date']} -> eligible Wk {p['earliest_return_week']}")
        return 0

    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
