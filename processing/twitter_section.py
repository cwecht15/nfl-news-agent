"""Build the AI Twitter Report section.

Powers `dashboard/pages/twitter_report.py` (on-demand, date-range reports).

Tweets arrive as `models.NewsItem` (source_type="twitter"). Unlike the news
pipeline's keyword team detection, this:

  1. Uses the LLM to attribute each tweet to a team — or "NFL" (league-wide), or
     nothing (off-topic) — even when no team name appears. This corrects keyword
     false positives (baseball "TB" → Buccaneers) and fills the un-teamed bucket.
  2. Reuses the per-team notes machinery (`_build_team_highlights_for_pool`),
     whose prompt clusters items reporting the SAME development into one bullet
     citing every source `[N]`, behind the SKIP quality gate.
  3. Produces a cross-team "Top Developments" highlights block (same clustering
     + `[N]` rules), modeled on `summarize_league_wide`.

The result shape parallels `processing.yt_section.build_yt_section` so the
dashboard render mirrors the YouTube / Podcast tabs. Returns {} for no tweets.
"""

import json
import logging
import re
import sys
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

from config_loader import get_teams_by_abbr
from models import NewsItem
from processing.summarizer import (
    _build_news_context_line,
    _build_team_highlights_for_pool,
    _call_model,
    _citation_source,
    _init_usage_tracker,
    _resolve_client_and_runtime,
    _trim_cited_sources,
)

logger = logging.getLogger(__name__)

# Tweets per attribution call. Output is just labels, so the cost is dominated
# by input; ~60 short tweets per call keeps each prompt well within limits.
_ATTRIB_BATCH = 60
# Max tweets fed to the cross-team highlights prompt (most recent win).
_HIGHLIGHT_LIMIT = 40


def _parse_attrib_json(raw: str, n: int) -> dict[int, list[str]]:
    """Parse the attribution model's reply into {1-based index: [LABELS]}.

    Tolerates code fences and stray prose around the JSON object.
    """
    s = (raw or "").strip()
    s = re.sub(r"^```(?:json)?", "", s).strip()
    s = re.sub(r"```$", "", s).strip()
    data: Any = None
    try:
        data = json.loads(s)
    except Exception:
        m = re.search(r"\{.*\}", s, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group(0))
            except Exception:
                data = None
    out: dict[int, list[str]] = {}
    if isinstance(data, dict):
        for k, v in data.items():
            try:
                idx = int(str(k).strip())
            except (TypeError, ValueError):
                continue
            if not (1 <= idx <= n):
                continue
            if isinstance(v, str):
                v = [v]
            if isinstance(v, list):
                out[idx] = [str(x).strip().upper() for x in v if str(x).strip()]
    return out


def _attribute_tweets(
    tweets: list[NewsItem],
    client: Any,
    runtime: dict[str, Any],
    usage_tracker: Optional[dict[str, Any]],
    valid: set[str],
) -> dict[int, list[str]]:
    """LLM team attribution. Returns {tweet-list-index: [TEAM|"NFL"]}.

    An empty/missing list for a tweet means off-topic / not NFL news — the
    caller drops it. Keyword tags are passed as a hint the model may override.
    """
    assignments: dict[int, list[str]] = {}
    codes = ", ".join(sorted(valid))
    for start in range(0, len(tweets), _ATTRIB_BATCH):
        batch = tweets[start:start + _ATTRIB_BATCH]
        lines = []
        for i, t in enumerate(batch, start=1):
            hint = ",".join(t.teams) if t.teams else "none"
            author = (t.author or t.source or "").strip()
            text = " ".join((t.title or "").split())[:260]
            lines.append(f'{i}. (author: {author} | keyword-hint: {hint}) {text}')

        prompt = f"""You are tagging NFL insider tweets by which team each is about.

Valid team codes: {codes}

For each numbered tweet, decide the team(s) it is PRIMARILY about and output their codes. Rules:
- Infer the team from player names, the beat writer's usual team, coaches, or context — even when the tweet never names the team. The keyword-hint is a guess you may override or ignore.
- A tweet can be about more than one team (e.g. a trade) — list each code.
- Use "NFL" (alone) for genuinely league-wide items with no single team (CBA, schedule, league-office, multi-team roundups).
- Use an empty list [] for tweets that are NOT NFL news (other sports, personal chatter, jokes, marketing) — these will be dropped.

Return ONLY a compact JSON object mapping each tweet number to its list of codes. No prose, no code fences. Example: {{"1": ["NYJ"], "2": ["NFL"], "3": [], "4": ["LAR","JAX"]}}

Tweets:
{chr(10).join(lines)}"""

        raw = _call_model(
            client,
            prompt,
            runtime,
            max_tokens=1500,
            usage_tracker=usage_tracker,
            usage_label="twitter:attribute",
            verbosity="low",
            reasoning_effort="low",
        )
        for i, labels in _parse_attrib_json(raw, len(batch)).items():
            assignments[start + i - 1] = labels
    return assignments


def _summarize_tweet_highlights(
    league_items: list[NewsItem],
    team_items: dict[str, list[NewsItem]],
    client: Any,
    runtime: dict[str, Any],
    usage_tracker: Optional[dict[str, Any]],
) -> dict:
    """Cross-team 'Top Developments' block. Mirrors summarize_league_wide."""
    by_url: dict[str, NewsItem] = {}
    for t in league_items:
        if t.url:
            by_url[t.url] = t
    for items in team_items.values():
        for t in items:
            if t.url:
                by_url.setdefault(t.url, t)

    pool = sorted(by_url.values(), key=lambda i: i.published, reverse=True)[:_HIGHLIGHT_LIMIT]
    if not pool:
        return {"summary": "", "count": 0, "numbered_sources": []}

    headlines, candidates = [], []
    for idx, item in enumerate(pool, start=1):
        headlines.append(f"[{idx}] " + _build_news_context_line(item, detail_chars=240))
        candidates.append({
            "num": idx, "title": item.title,
            "source": _citation_source(item), "url": item.url,
        })

    prompt = f"""Write the "Top Developments" highlights for an NFL insider-tweet report — the most newsworthy items across the whole league.

Each input tweet is numbered like [1], [2] — you MUST cite the tweets you use.

Output: a markdown bullet list, most important first.
- MERGE tweets reporting the SAME development into ONE bullet and cite every source, e.g. [1, 4, 7].
- Lead each bullet with the named subject (player, coach, or team) in bold, then 1–2 sentences on what happened and why it matters.
- Prioritize transactions, injuries, depth-chart / role changes, and notable insider scoops.
- End each bullet with its citation(s). Cap at 8 bullets — drop the lowest-signal items beyond that.
- Use ONLY the tweets provided; never invent contract terms, timelines, or quotes.
- No headers, no preamble, no closing commentary.

Tweets:
{chr(10).join(headlines)}"""

    text = _call_model(
        client,
        prompt,
        runtime,
        max_tokens=3000,
        usage_tracker=usage_tracker,
        usage_label="twitter:highlights",
        verbosity="medium",
        reasoning_effort="low",
    )
    return {
        "summary": text,
        "count": len(pool),
        "numbered_sources": _trim_cited_sources(text, candidates),
    }


def build_twitter_section(
    tweets: list[NewsItem],
    client: Optional[Any] = None,
    usage_tracker: Optional[dict[str, Any]] = None,
    date_label: str = "",
) -> dict:
    """Summarize tweets into the Twitter report section.

    Args:
        tweets: Tweets to summarize. The caller is expected to have already
            scrubbed promo/holiday fluff (`quality_filter.filter_news_items`);
            off-topic tweets are dropped here by the attribution step.
        client / usage_tracker: optional, as in `build_yt_section`.
        date_label: free-form label for logging / display.

    Returns a dict shaped like `build_yt_section`:
        {
            "highlights": {"summary", "count", "numbered_sources"},
            "team_notes": {team_abbr: {"summary", "numbered_sources"}},
            "date_label", "tweet_count", "attributed_teams", "league_wide_count",
            "llm_usage": {...},  # only when this function owns the tracker
        }
        or {} when tweets is empty.
    """
    if not tweets:
        return {}

    client, runtime = _resolve_client_and_runtime(client)
    own_tracker = usage_tracker is None
    if own_tracker:
        usage_tracker = _init_usage_tracker(runtime)

    logger.info(
        "Building Twitter section: %d tweets (%s)",
        len(tweets), date_label or "no label",
    )

    valid = set(get_teams_by_abbr().keys())
    assignments = _attribute_tweets(tweets, client, runtime, usage_tracker, valid)

    team_items: dict[str, list[NewsItem]] = {}
    league_items: list[NewsItem] = []
    dropped = 0
    for i, tweet in enumerate(tweets):
        labels = assignments.get(i, [])
        teams = [lab for lab in labels if lab in valid]
        if teams:
            for tm in teams:
                team_items.setdefault(tm, []).append(tweet)
        elif "NFL" in labels:
            league_items.append(tweet)
        else:
            dropped += 1

    logger.info(
        "Twitter attribution: %d teams, %d league-wide, %d dropped (off-topic)",
        len(team_items), len(league_items), dropped,
    )

    team_notes = _build_team_highlights_for_pool(
        team_items, client, runtime, usage_tracker,
    )
    highlights = _summarize_tweet_highlights(
        league_items, team_items, client, runtime, usage_tracker,
    )

    result: dict[str, Any] = {
        "highlights": highlights,
        "team_notes": team_notes,
        "date_label": date_label,
        "tweet_count": len(tweets),
        "attributed_teams": len(team_items),
        "league_wide_count": len(league_items),
    }
    if own_tracker:
        result["llm_usage"] = usage_tracker
    return result
