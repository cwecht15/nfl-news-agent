"""Twitter Report — browsable insider-tweet list, with an optional AI summary.

Pairs with `scripts/collect_twitter.py` / the daily pipeline: those collect
tweets into `data/raw/<date>/twitter.json`; this page browses whatever's in
the selected range.

The primary view is the **Tweet List** tab — no LLM, no tokens: pick a date
range, filter by team / player / keyword, sort, and read the raw tweets with
promo/holiday fluff and off-topic noise scrubbed out.

The **AI Summary** tab keeps the on-demand LLM report
(`processing.twitter_section.build_twitter_section`):
  * uses the LLM to place each tweet on the right team even when no team is
    named (and to drop off-topic tweets),
  * clusters tweets reporting the same development into one bullet citing every
    source [N].
"""

import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import streamlit as st

st.set_page_config(page_title="Twitter Report", page_icon="🐦", layout="wide")

from dashboard.auth import require_password
require_password()

from dashboard.helpers import bootstrap_secrets
bootstrap_secrets()

from config_loader import get_data_dir, get_teams_by_abbr
from dashboard.citations import build_citation_linker
from dashboard.helpers import to_et_display
from models import NewsItem
from processing import twitter_cache
from processing.quality_filter import filter_news_items
from processing.summarizer import (
    _TWITTER_LEAGUE_SIGNAL,
    _league_wide_eligible,
    _load_position_lookup,
)
from processing.twitter_section import build_twitter_section

raw_base = get_data_dir("raw")

st.header("Twitter Report")

st.caption(
    "Insider-list tweets, scrubbed of promo/off-topic junk. Browse the raw "
    "list by team, player, or keyword — no LLM involved. The **AI Summary** "
    "tab still offers the clustered, team-attributed report on demand."
)


# ──────────────────────────────────────────────────────────
# Load
# ──────────────────────────────────────────────────────────

def _list_dated_dirs() -> list[str]:
    dirs: list[str] = []
    if not raw_base.exists():
        return dirs
    for d in raw_base.iterdir():
        if not d.is_dir() or len(d.name) != 10 or d.name[4] != "-":
            continue
        if (d / "twitter.json").exists():
            dirs.append(d.name)
    return sorted(dirs)


@st.cache_data(ttl=120, show_spinner=False)
def _load_all_tweets() -> list[NewsItem]:
    """Every tweet across all collection folders, deduped by URL.

    Filtered by the tweet's own published date (not the folder date) so the
    range picker is meaningful — one backfill can drop many days in one folder.
    """
    out: list[NewsItem] = []
    seen: set[str] = set()
    for dir_name in _list_dated_dirs():
        path = raw_base / dir_name / "twitter.json"
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception:
            continue
        for record in raw:
            try:
                item = NewsItem.from_dict(record)
            except Exception:
                continue
            if item.url and item.url in seen:
                continue
            if item.url:
                seen.add(item.url)
            out.append(item)
    return out


@st.cache_data(ttl=600, show_spinner=False)
def _player_names() -> set[str]:
    return {n for n in _load_position_lookup() if " " in n}


def _tweet_dt(item: NewsItem) -> date | None:
    return item.published.date() if item.published else None


def _parse_date(s: str) -> date | None:
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None


def _scrubbed_in_range(start: date, end: date) -> list[NewsItem]:
    """Tweets in [start, end] with promo/holiday fluff (#2) removed."""
    items = [
        i for i in _load_all_tweets()
        if (d := _tweet_dt(i)) is not None and start <= d <= end
    ]
    kept, _ = filter_news_items(items)
    return kept


@st.cache_data(ttl=600, show_spinner=False)
def _players_mentioned(ids_tuple: tuple) -> list[str]:
    """Known player names (lowercase) appearing in the given tweets."""
    wanted = set(ids_tuple)
    players = _player_names()
    found: set[str] = set()
    for t in _load_all_tweets():
        if t.url not in wanted:
            continue
        low = (t.title or "").lower()
        for name in players:
            if name in low:
                found.add(name)
    return sorted(found)


@st.cache_data(ttl=86400, show_spinner=False)
def _generate_cached(start_iso: str, end_iso: str, ids_tuple: tuple, force: bool = False) -> dict:
    """Build (or load from disk cache) the Twitter report for a range.

    `force=True` skips the disk cache and re-summarizes (spends tokens) — used
    after a logic change. `force` is part of the signature so it also keys the
    in-memory @st.cache_data layer.
    """
    start, end = _parse_date(start_iso), _parse_date(end_iso)
    if start is None or end is None or end < start:
        return {}
    wanted = set(ids_tuple)
    tweets = [t for t in _scrubbed_in_range(start, end) if t.url in wanted]
    if not tweets:
        return {"empty": True}

    key = twitter_cache.make_key(start_iso, end_iso, [], ids_tuple)
    if not force:
        cached = twitter_cache.load(key)
        if cached and cached.get("section"):
            section = cached["section"]
            section["_cache"] = {"hit": True, "generated_at": cached.get("generated_at")}
            return section

    label = start_iso if start_iso == end_iso else f"{start_iso} → {end_iso}"
    section = build_twitter_section(tweets, date_label=label)
    twitter_cache.save(
        key, section,
        meta={"start": start_iso, "end": end_iso, "tweet_count": len(tweets)},
    )
    section["_cache"] = {"hit": False}
    return section


# ──────────────────────────────────────────────────────────
# UI — date range
# ──────────────────────────────────────────────────────────

if not _list_dated_dirs():
    st.warning(
        "No tweets available yet. Run `python scripts/collect_twitter.py` "
        "locally (or let the daily pipeline run) and push the resulting "
        "`data/raw/<date>/twitter.json` files."
    )
    st.stop()

all_items = _load_all_tweets()
dates = [d for d in (_tweet_dt(i) for i in all_items) if d is not None]
if not dates:
    st.warning("No tweets with a parseable date yet.")
    st.stop()

earliest, latest = min(dates), max(dates)
today = date.today()
default_end = max(earliest, min(latest, today))
default_start = max(earliest, min(latest, default_end - timedelta(days=1)))

for _k in ("tw_start_date", "tw_end_date"):
    _v = st.session_state.get(_k)
    if isinstance(_v, date) and not (earliest <= _v <= latest):
        del st.session_state[_k]

col_l, col_r = st.columns(2)
with col_l:
    start_d = st.date_input("Start date", value=default_start,
                            min_value=earliest, max_value=latest, key="tw_start_date")
with col_r:
    end_d = st.date_input("End date", value=default_end,
                          min_value=earliest, max_value=latest, key="tw_end_date")

if start_d > end_d:
    st.error("Start date must be on or before end date.")
    st.stop()

st.caption(f"Tweets available (by published date): {earliest.isoformat()} → {latest.isoformat()}")

scrubbed = _scrubbed_in_range(start_d, end_d)
ids_tuple = tuple(sorted(t.url for t in scrubbed if t.url))
if not scrubbed:
    st.info("No tweets in this date range (after fluff scrub). Widen the range or push more.")
    st.stop()

all_team_abbrs = sorted(get_teams_by_abbr().keys())

tab_list, tab_ai = st.tabs(["📋 Tweet List", "🤖 AI Summary"])


# ──────────────────────────────────────────────────────────
# Tab 1 — raw tweet list (no LLM)
# ──────────────────────────────────────────────────────────

def _render_tweet(item: NewsItem, show_teams: bool = False) -> None:
    when = to_et_display(item.published.isoformat()) if item.published else ""
    author = item.author or item.source
    byline = f"**{author}**"
    if show_teams and item.teams:
        byline += f"  ·  {'/'.join(item.teams)}"
    if when:
        byline += f"  ·  {when}"
    body = byline + "  \n" + (item.title or "")
    if item.url:
        body += f"  \n[Open tweet ↗]({item.url})"
    st.markdown(body)
    st.divider()


with tab_list:
    fcol1, fcol2 = st.columns(2)
    with fcol1:
        team_filter = st.multiselect(
            "Teams (empty = all)", all_team_abbrs, key="tw_list_teams",
            help="Keyword-detected team tags. Tweets naming no team appear "
                 "under 'General / no team detected'.",
        )
    with fcol2:
        mentioned = _players_mentioned(ids_tuple)
        player_filter = st.multiselect(
            "Players (empty = all)", [n.title() for n in mentioned],
            key="tw_list_players",
            help="Players from the latest depth chart mentioned by name in "
                 "tweets in this range.",
        )

    fcol3, fcol4, fcol5 = st.columns([3, 2, 2])
    with fcol3:
        search = st.text_input(
            "Search text / author", placeholder="e.g. Schefter, contract, WR",
            key="tw_raw_search",
            help="Comma-separate terms to match any of them (e.g. `holdout, extension`).",
        )
    with fcol4:
        view = st.selectbox("View", ["Grouped by team", "Newest first", "Oldest first"],
                            key="tw_list_view")
    with fcol5:
        scrub_offtopic = st.checkbox(
            "Scrub off-topic / non-news", value=True, key="tw_raw_scrub",
            help="Hide un-teamed tweets with no transaction/injury/roster/player signal.",
        )

    players = _player_names()
    terms = [t.strip().lower() for t in search.split(",") if t.strip()]
    wanted_players = [p.lower() for p in player_filter]

    def _match(item: NewsItem) -> bool:
        low = (item.title or "").lower()
        if wanted_players and not any(p in low for p in wanted_players):
            return False
        if terms:
            author_low = (item.author or "").lower()
            if not any(t in low or t in author_low for t in terms):
                return False
        return True

    by_team: dict[str, list[NewsItem]] = {}
    untagged: list[NewsItem] = []
    offtopic = 0
    matched: list[NewsItem] = []
    for item in scrubbed:
        if not _match(item):
            continue
        if item.teams:
            if team_filter and not set(item.teams) & set(team_filter):
                continue
            matched.append(item)
            for t in item.teams:
                if team_filter and t not in set(team_filter):
                    continue
                by_team.setdefault(t, []).append(item)
        elif team_filter:
            continue  # team filter means specific teams only
        elif scrub_offtopic and not _league_wide_eligible(item, players, _TWITTER_LEAGUE_SIGNAL):
            offtopic += 1
        else:
            matched.append(item)
            untagged.append(item)

    st.caption(
        f"Showing {len(matched)} of {len(scrubbed)} tweets in range · "
        f"team-tagged: {sum(len(v) for v in by_team.values())} across {len(by_team)} teams · "
        f"un-teamed: {len(untagged)} · off-topic hidden: {offtopic}. "
        "(Team tags are keyword-detected; the AI Summary re-attributes with the LLM.)"
    )

    if view == "Grouped by team":
        for team in sorted(by_team, key=lambda t: (-len(by_team[t]), t)):
            items = sorted(by_team[team], key=lambda i: i.published, reverse=True)
            with st.expander(f"{team} ({len(items)})", expanded=bool(team_filter or wanted_players or terms)):
                for item in items:
                    _render_tweet(item)
        if untagged:
            items = sorted(untagged, key=lambda i: i.published, reverse=True)
            with st.expander(f"General / no team detected ({len(items)})", expanded=False):
                for item in items:
                    _render_tweet(item)
    else:
        newest_first = view == "Newest first"
        for item in sorted(matched, key=lambda i: i.published, reverse=newest_first):
            _render_tweet(item, show_teams=True)

    if not matched:
        st.info("No tweets match the current filters.")


# ──────────────────────────────────────────────────────────
# Tab 2 — AI summary (on-demand LLM)
# ──────────────────────────────────────────────────────────

def _render_numbered_sources(numbered: list[dict]) -> None:
    if not numbered:
        return
    lines = []
    for src in numbered:
        num = src.get("num", "")
        title_text = src.get("title", "Source")
        source_name = src.get("source", "")
        url = src.get("url", "")
        suffix = f" ({source_name})" if source_name else ""
        if url:
            lines.append(f"**[{num}]** [{title_text}]({url}){suffix}")
        else:
            lines.append(f"**[{num}]** {title_text}{suffix}")
    st.caption("Sources")
    st.markdown("\n\n".join(lines))


with tab_ai:
    if not os.environ.get("OPENAI_API_KEY"):
        st.error(
            "**OpenAI API key is not configured for this dashboard.** The AI "
            "Summary calls OpenAI on demand to summarize tweets, so a key has "
            "to be reachable to the Streamlit process. (The Tweet List tab "
            "works without one.)\n\n"
            "**On Streamlit Cloud:** *Manage app → Settings → Secrets*, add "
            "`OPENAI_API_KEY = \"sk-...\"`, save, and reload."
        )
    else:
        st.caption(
            "The model places each tweet on the right team (even with no team "
            "named), clusters tweets about the same story, and cites every "
            "source. Each click runs LLM calls; results are cached."
        )

        ai_team_filter = st.multiselect(
            "Filter teams (leave empty for all)", all_team_abbrs, key="tw_ai_teams",
            help="Filters which Per-Team Notes are shown. The full range is "
                 "always summarized so attribution stays accurate.",
        )

        st.caption(f"{len(scrubbed)} tweets in range after fluff scrub — ready to summarize.")

        session_key = "tw_report_section"
        inputs_key = "tw_report_inputs"
        current_inputs = (start_d.isoformat(), end_d.isoformat(), ids_tuple)

        gen_col, force_col = st.columns([1, 2])
        with gen_col:
            generate = st.button("Generate Report", type="primary")
        with force_col:
            force = st.checkbox(
                "Force regenerate (ignore cache)", value=False,
                help="Re-summarize from scratch instead of loading the saved result — "
                     "spends tokens. Use after a logic/label change.",
            )

        if generate:
            if force:
                # Bust the in-memory memo so a later non-force view also picks up the
                # freshly-written disk cache instead of the stale entry.
                _generate_cached.clear()
            with st.spinner("Summarizing tweets (attributing teams, clustering stories)..."):
                section = _generate_cached(start_d.isoformat(), end_d.isoformat(), ids_tuple, force)
            st.session_state[session_key] = section
            st.session_state[inputs_key] = current_inputs
        else:
            section = st.session_state.get(session_key)
            if section is not None and st.session_state.get(inputs_key) != current_inputs:
                section = None
                st.session_state.pop(session_key, None)
                st.session_state.pop(inputs_key, None)

        if section and not section.get("empty"):
            cache_info = section.get("_cache", {}) or {}
            count = section.get("tweet_count", 0)
            label = section.get("date_label", "")
            st.success(f"Summarized {count} tweets ({label}).")
            if cache_info.get("hit"):
                st.caption(
                    f"⚡ Loaded from saved cache (generated {cache_info.get('generated_at', '')} "
                    f"UTC) — no tokens spent on this view."
                )

            highlights = section.get("highlights", {}) or {}
            hl_summary = (highlights.get("summary") or "").strip()
            if hl_summary:
                st.subheader("Top Developments")
                hl_numbered = highlights.get("numbered_sources", []) or []
                hl_linkify = build_citation_linker(hl_numbered)
                st.markdown(hl_linkify(hl_summary) if hl_linkify else hl_summary,
                            unsafe_allow_html=True)
                _render_numbered_sources(hl_numbered)

            team_notes = section.get("team_notes", {}) or {}
            show_teams = sorted(t for t in team_notes
                                if (not ai_team_filter or t in set(ai_team_filter)))
            if show_teams:
                st.subheader("Per-Team Notes")
                for team in show_teams:
                    note = team_notes[team]
                    note_summary = (note.get("summary") or "").strip()
                    if not note_summary:
                        continue
                    numbered = note.get("numbered_sources", []) or []
                    note_linkify = build_citation_linker(numbered)
                    st.markdown(f"**{team}**")
                    st.markdown(note_linkify(note_summary) if note_linkify else note_summary,
                                unsafe_allow_html=True)
                    _render_numbered_sources(numbered)
                    st.divider()
            elif ai_team_filter:
                st.info("No team notes for the selected team(s) in this range.")

            usage = section.get("llm_usage", {}) or {}
            if usage:
                st.subheader("LLM Usage")
                cols = st.columns(5)
                cols[0].metric("Calls", usage.get("request_count", 0))
                cols[1].metric("Input", usage.get("input_tokens", 0))
                cols[2].metric("Output", usage.get("output_tokens", 0))
                cols[3].metric("Reasoning", usage.get("reasoning_tokens", 0))
                cols[4].metric("Est. Cost", f"${float(usage.get('estimated_cost_usd', 0.0)):.4f}")
        elif section and section.get("empty"):
            st.info("No tweets in that range. Push more before regenerating.")
        else:
            st.info("Pick a range and click **Generate Report**. Each generation runs "
                    "LLM calls (then it's cached).")
