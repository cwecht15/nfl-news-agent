"""FantasyPoints Articles — searchable archive of pulled articles.

Loads `data/raw/<date>/fantasypoints.json` files within a date range and
lets the user search the raw article text for a player or team. No LLM
summarization — every match shows the actual paragraph as written by
the FantasyPoints author.
"""

import json
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import streamlit as st

st.set_page_config(page_title="FantasyPoints Articles", page_icon="🏈", layout="wide")

from dashboard.auth import require_password
require_password()

from config_loader import get_data_dir, get_teams_by_abbr


st.header("FantasyPoints Articles")
st.caption(
    "Searchable archive of NFL articles pulled from the FantasyPoints API. "
    "Pick a date range and search for a player or team — every paragraph "
    "from any matching article that mentions them is shown verbatim."
)

raw_base = get_data_dir("raw")


# ──────────────────────────────────────────────────────────
# Loading
# ──────────────────────────────────────────────────────────

def _list_dated_dirs() -> list[str]:
    """Return YYYY-MM-DD folders that have a fantasypoints.json file."""
    out: list[str] = []
    if not raw_base.exists():
        return out
    for d in raw_base.iterdir():
        if not d.is_dir() or len(d.name) != 10 or d.name[4] != "-":
            continue
        if (d / "fantasypoints.json").exists():
            out.append(d.name)
    return sorted(out)


def _parse_date(s: str) -> date | None:
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None


def _article_id(article: dict) -> str:
    """Stable per-article identifier resilient to URL format changes.

    Uses the URL's final path slug (case-folded) when available so a
    legacy `/articles/<slug>` and the current `/articles/<year>/<slug>#/`
    shape collapse to the same key. Falls back to title+author when the
    URL is missing.
    """
    url = (article.get("url") or "").strip()
    if url:
        url = url.split("#", 1)[0].rstrip("/")
        if "/" in url:
            slug = url.rsplit("/", 1)[-1]
            if slug:
                return f"slug:{slug.lower()}"
    title = (article.get("title") or "").strip().lower()
    author = (article.get("author") or "").strip().lower()
    return f"ta:{title}|{author}"


@st.cache_data(ttl=600, show_spinner=False)
def _load_articles_in_range(start_iso: str, end_iso: str) -> list[dict]:
    """Load every fantasypoints article whose folder date is in [start, end].

    Returns a list of dicts with the on-disk shape plus a `_collected_date`
    key so callers can group/display by collection date. Deduplicates by
    URL slug so an article whose URL changed across runs (e.g. before vs
    after the 2026-05-01 URL builder fix) only renders once.
    """
    start = _parse_date(start_iso)
    end = _parse_date(end_iso)
    if start is None or end is None or end < start:
        return []

    seen_ids: set[str] = set()
    out: list[dict] = []
    for dir_name in _list_dated_dirs():
        dir_date = _parse_date(dir_name)
        if dir_date is None or dir_date < start or dir_date > end:
            continue
        path = raw_base / dir_name / "fantasypoints.json"
        try:
            with open(path, "r", encoding="utf-8") as f:
                items = json.load(f)
        except Exception:
            continue
        for item in items:
            aid = _article_id(item)
            if aid in seen_ids:
                continue
            seen_ids.add(aid)
            item["_collected_date"] = dir_name
            out.append(item)
    return out


# ──────────────────────────────────────────────────────────
# Search / matching
# ──────────────────────────────────────────────────────────

_PARA_SPLIT = re.compile(r"\n\s*\n")
_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"“])")


def _split_into_blurbs(text: str) -> list[str]:
    """Split body into reasonable display units.

    First by blank-line paragraphs (markdown convention). When a
    paragraph is itself huge (some articles are one giant paragraph),
    fall back to sentence-level splits inside that paragraph so the
    returned blurb isn't an entire article.
    """
    if not text:
        return []
    blurbs: list[str] = []
    for para in _PARA_SPLIT.split(text):
        para = para.strip()
        if not para:
            continue
        if len(para) <= 700:
            blurbs.append(para)
            continue
        # Long paragraph → break into sentence-sized pieces, packing
        # consecutive sentences up to ~600 chars per blurb so context
        # is preserved.
        sentences = _SENT_SPLIT.split(para)
        bucket = ""
        for sent in sentences:
            sent = sent.strip()
            if not sent:
                continue
            if len(bucket) + len(sent) + 1 > 600 and bucket:
                blurbs.append(bucket.strip())
                bucket = sent
            else:
                bucket = (bucket + " " + sent).strip() if bucket else sent
        if bucket:
            blurbs.append(bucket.strip())
    return blurbs


def _matches(blurb: str, needle_re: re.Pattern | None) -> bool:
    """True when blurb matches the search regex (or no search active)."""
    if needle_re is None:
        return True
    return bool(needle_re.search(blurb))


def _highlight(blurb: str, needle_re: re.Pattern | None) -> str:
    """Wrap regex matches in **bold** for Streamlit markdown rendering."""
    if needle_re is None:
        return blurb
    return needle_re.sub(lambda m: f"**{m.group(0)}**", blurb)


def _phrase_pattern(phrase: str) -> str | None:
    """Build a single phrase regex fragment (no anchoring/grouping)."""
    tokens = re.findall(r"\w+", phrase or "")
    if not tokens:
        return None
    return r"\b" + r"[\s\W_]+".join(re.escape(tok) for tok in tokens) + r"\b"


def _build_search_regex(phrases: str | list[str]) -> re.Pattern | None:
    """Build a case-insensitive regex matching ANY of the supplied phrases.

    Accepts a single string (one phrase) or a list. Returns None when
    nothing usable was supplied. Whole-word match; punctuation between
    tokens of a multi-word phrase is tolerated (so "K.C." matches
    "K. C." too).
    """
    if isinstance(phrases, str):
        phrases = [phrases]
    fragments: list[str] = []
    seen: set[str] = set()
    for phrase in phrases:
        frag = _phrase_pattern(phrase)
        if frag and frag not in seen:
            seen.add(frag)
            fragments.append(frag)
    if not fragments:
        return None
    pattern = "(?:" + "|".join(fragments) + ")" if len(fragments) > 1 else fragments[0]
    try:
        return re.compile(pattern, re.IGNORECASE)
    except re.error:
        return None


def _expand_query_for_teams(
    query: str,
    teams_by_abbr: dict,
) -> tuple[list[str], list[str]]:
    """Auto-expand a query that exactly matches a team alias.

    "CLE", "Cleveland", "Browns", and "Cleveland Browns" all expand to
    the same alias set so any of them surface every Cleveland mention.
    Matching is exact-alias (case-insensitive), not substring — so
    "Brown" stays a literal text search for the Pro Bowler's name.

    Returns (phrases, matched_team_abbrs). When no team alias matches,
    returns ([query], []) so the caller falls back to plain text search.
    """
    q = (query or "").strip().lower()
    if not q:
        return ([], [])

    def _aliases_for(team: dict, abbr: str) -> list[str]:
        name = (team.get("name") or "").strip()
        parts = name.split()
        nickname = parts[-1] if parts else ""
        city = " ".join(parts[:-1]) if len(parts) > 1 else ""
        return [a for a in (abbr, name, nickname, city) if a]

    matched_abbrs: list[str] = []
    for abbr, team in teams_by_abbr.items():
        for alias in _aliases_for(team, abbr):
            if alias.lower() == q:
                matched_abbrs.append(abbr)
                break

    if not matched_abbrs:
        return ([query], [])

    expanded: list[str] = []
    seen: set[str] = set()
    for abbr in matched_abbrs:
        for alias in _aliases_for(teams_by_abbr[abbr], abbr):
            key = alias.lower()
            if key not in seen:
                seen.add(key)
                expanded.append(alias)
    return (expanded, matched_abbrs)


# ──────────────────────────────────────────────────────────
# UI
# ──────────────────────────────────────────────────────────

available_dirs = _list_dated_dirs()
if not available_dirs:
    st.warning(
        "No FantasyPoints articles in the archive yet. The daily pipeline "
        "starts populating `data/raw/<date>/fantasypoints.json` once "
        "`FANTASYPOINTS_AUTH` and `FANTASYPOINTS_SOURCE` are configured."
    )
    st.stop()

earliest = _parse_date(available_dirs[0]) or date.today() - timedelta(days=30)
latest = _parse_date(available_dirs[-1]) or date.today()

today = date.today()
default_end = min(latest, today)
default_start = max(earliest, default_end - timedelta(days=6))

col_l, col_r = st.columns(2)
with col_l:
    start_d = st.date_input(
        "Start date", value=default_start,
        min_value=earliest, max_value=latest,
        key="fp_start",
    )
with col_r:
    end_d = st.date_input(
        "End date", value=default_end,
        min_value=earliest, max_value=latest,
        key="fp_end",
    )

if start_d > end_d:
    st.error("Start date must be on or before end date.")
    st.stop()

teams_by_abbr = get_teams_by_abbr()
all_team_abbrs = sorted(teams_by_abbr.keys())

col_q, col_t = st.columns([2, 1])
with col_q:
    query = st.text_input(
        "Search player or text",
        value="",
        placeholder="e.g. Jeremiyah Love, Concepcion, Saquon Barkley",
        help="Case-insensitive phrase match across article titles + bodies. "
             "Leave blank to browse all articles in the range.",
        key="fp_query",
    )
with col_t:
    team_filter_list = st.multiselect(
        "Filter team(s)",
        all_team_abbrs,
        default=[],
        help="Restricts to articles that mention any selected team.",
        key="fp_teams",
    )

st.caption(
    f"Archive: {available_dirs[0]} → {available_dirs[-1]} "
    f"({len(available_dirs)} days collected)"
)


# ──────────────────────────────────────────────────────────
# Run search
# ──────────────────────────────────────────────────────────

articles = _load_articles_in_range(start_d.isoformat(), end_d.isoformat())

if team_filter_list:
    wanted_teams = set(team_filter_list)
    articles = [a for a in articles if wanted_teams.intersection(a.get("teams") or [])]

# When the search query *itself* names a team (CLE / Cleveland / Browns
# / Cleveland Browns), expand to the full alias set so any variant
# surfaces every mention. Falls through to a plain literal phrase
# search otherwise.
search_phrases, matched_team_abbrs = _expand_query_for_teams(query, teams_by_abbr)
needle = _build_search_regex(search_phrases) if search_phrases else None

if matched_team_abbrs:
    team_labels = ", ".join(
        teams_by_abbr[a]["name"] for a in matched_team_abbrs
        if a in teams_by_abbr
    )
    alias_preview = ", ".join(f"`{p}`" for p in search_phrases)
    st.caption(
        f"Treating `{query}` as a team search → {team_labels} "
        f"(matching: {alias_preview})"
    )

# Per-article match info: (article, matching_blurbs, matched_in_title)
results: list[tuple[dict, list[str], bool]] = []
for article in articles:
    title = article.get("title", "") or ""
    body = article.get("full_text") or article.get("summary") or ""
    blurbs = _split_into_blurbs(body)

    matched_in_title = bool(needle and needle.search(title))
    if needle is None:
        # No query → keep every article, every blurb.
        results.append((article, blurbs, False))
        continue

    matched_blurbs = [b for b in blurbs if needle.search(b)]
    if matched_in_title or matched_blurbs:
        # If only the title matched, surface the first blurb as preview
        # so the reader still sees the author's actual prose.
        if matched_in_title and not matched_blurbs and blurbs:
            matched_blurbs = blurbs[:1]
        results.append((article, matched_blurbs, matched_in_title))

# Sort newest first by published timestamp (fallback to collected date).
def _sort_key(rec: tuple[dict, list[str], bool]) -> str:
    article = rec[0]
    return article.get("published") or article.get("_collected_date") or ""


results.sort(key=_sort_key, reverse=True)

# ──────────────────────────────────────────────────────────
# Render
# ──────────────────────────────────────────────────────────

if not results:
    if query:
        st.info(
            f"No articles in {start_d} → {end_d} match `{query}`"
            + (f" for team(s) {', '.join(team_filter_list)}" if team_filter_list else "")
            + "."
        )
    else:
        st.info(f"No FantasyPoints articles collected between {start_d} and {end_d}.")
    st.stop()

total_blurbs = sum(len(blurbs) for _, blurbs, _ in results)
header_bits = [f"{len(results)} article{'s' if len(results) != 1 else ''}"]
if query:
    header_bits.append(f"{total_blurbs} matching paragraph{'s' if total_blurbs != 1 else ''}")
st.subheader(" · ".join(header_bits))

def _article_meta_line(article: dict) -> str:
    """Build a one-line article header with author, link, date, and teams."""
    title = article.get("title", "Untitled")
    url = article.get("url", "")
    author = article.get("author", "")
    published_iso = article.get("published") or ""
    teams_list = article.get("teams") or []

    try:
        published_dt = datetime.fromisoformat(published_iso) if published_iso else None
    except ValueError:
        published_dt = None
    published_label = (
        published_dt.strftime("%Y-%m-%d") if published_dt
        else article.get("_collected_date", "")
    )

    title_md = f"[{title}]({url})" if url else title
    bits: list[str] = []
    if author:
        bits.append(f"**{author}**")
    bits.append(f"*{title_md}*")
    if published_label:
        bits.append(published_label)
    if teams_list:
        bits.append(" / ".join(sorted(teams_list)))
    return " · ".join(bits)


# Search mode → flat bullets (one block per article: meta line + matching
# excerpts as blockquotes directly underneath, no per-article expander).
# Browse mode (no query) → keep per-article expanders so 9+ full articles
# don't render as a single wall of text.

if query:
    for article, matching_blurbs, matched_in_title in results:
        st.markdown(_article_meta_line(article))
        if matched_in_title and not matching_blurbs:
            st.caption("Match in title only")
        for blurb in matching_blurbs:
            st.markdown(f"> {_highlight(blurb, needle)}")
        st.divider()
else:
    for article, matching_blurbs, _ in results:
        title = article.get("title", "Untitled")
        published_iso = article.get("published") or ""
        try:
            published_dt = datetime.fromisoformat(published_iso) if published_iso else None
        except ValueError:
            published_dt = None
        published_label = (
            published_dt.strftime("%Y-%m-%d") if published_dt
            else article.get("_collected_date", "")
        )
        with st.expander(f"{published_label} — {title}", expanded=False):
            st.markdown(_article_meta_line(article))
            full_body = article.get("full_text") or article.get("summary") or ""
            if full_body:
                st.markdown(full_body)
