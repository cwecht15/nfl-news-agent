"""Report builder.

Assembles the daily report from summarized data into:
1. JSON (for dashboard consumption)
2. Standalone HTML (viewable in any browser)
"""

import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

from jinja2 import Template

from config_loader import get_data_dir, get_settings
from models import DailyReport, NewsItem
from processing.deduplicator import is_primary_source

logger = logging.getLogger(__name__)

DEFAULT_SECTION_SOURCE_LIMIT = 8
TEAM_SOURCE_LIMIT = 6
LEAGUE_WIDE_SOURCE_LIMIT = 8
PROJECTION_MOVER_LIMIT = 15

HTML_TEMPLATE = Template(
    """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>NFL Daily Report - {{ date }}</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            line-height: 1.6; color: #1a1a1a; max-width: 900px; margin: 0 auto;
            padding: 2rem 1rem; background: #f8f9fa;
        }
        h1 { color: #013369; border-bottom: 3px solid #d50a0a; padding-bottom: 0.5rem; margin-bottom: 1.5rem; }
        h2 { color: #013369; margin-top: 2rem; margin-bottom: 0.75rem; }
        h3 { color: #333; margin-top: 1rem; margin-bottom: 0.5rem; }
        a { color: #0b57d0; text-decoration: none; }
        a:hover { text-decoration: underline; }
        .section {
            background: white; border-radius: 8px; padding: 1.5rem;
            margin-bottom: 1.5rem; box-shadow: 0 1px 3px rgba(0,0,0,0.1);
        }
        .section-header {
            display: flex; align-items: center; gap: 0.5rem;
            margin-bottom: 1rem; padding-bottom: 0.5rem; border-bottom: 1px solid #e0e0e0;
        }
        .badge {
            background: #013369; color: white; font-size: 0.75rem;
            padding: 0.2rem 0.5rem; border-radius: 4px;
        }
        .team-tag {
            display: inline-block; background: #e8f0fe; color: #013369;
            font-size: 0.8rem; padding: 0.15rem 0.4rem; border-radius: 3px;
            margin: 0.1rem;
        }
        .sources {
            margin-top: 1rem; padding-top: 0.75rem; border-top: 1px solid #e8eaed;
        }
        .sources ul {
            padding-left: 1.25rem;
            margin-top: 0.35rem;
        }
        .source-meta {
            color: #666;
            font-size: 0.9rem;
        }
        .stats { color: #666; font-size: 0.85rem; margin-top: 2rem; }
        .generated { color: #999; font-size: 0.8rem; text-align: center; margin-top: 2rem; }
        ul { padding-left: 1.5rem; }
        li { margin-bottom: 0.3rem; }
        p { margin-bottom: 0.75rem; }
    </style>
</head>
<body>
    <h1>NFL Daily Report - {{ date }}</h1>
    {% if season_meta and season_meta.get('week') %}
    <div class="generated" style="margin-bottom:16px;">
        Week {{ season_meta.week }}{% if season_meta.get('day_role') %} &middot; {{ season_meta.day_role }}{% endif %}{% if season_meta.get('active_sheet') %} &middot; working sheet: {{ season_meta.active_sheet }}{% endif %}{% if pm_updated_at %} &middot; evening update {{ pm_updated_at }}{% endif %}
    </div>
    {% endif %}

    {% for section_key, section in sections.items() %}
    <div class="section">
        <div class="section-header">
            <h2>{{ section_titles.get(section_key, section_key) }}</h2>
            {% if section.get('count') %}
            <span class="badge">{{ section.count }} items</span>
            {% endif %}
        </div>
        <div>{{ section.summary | replace('\\n', '<br>') }}</div>
        {% if section.get('sources') %}
        <div class="sources">
            <strong>Sources</strong>
            <ul>
                {% for source in section.sources %}
                <li>
                    <a href="{{ source.url }}" target="_blank" rel="noopener noreferrer">{{ source.title }}</a>
                    {% if source.get('source') %}
                    <span class="source-meta">({{ source.source }})</span>
                    {% endif %}
                </li>
                {% endfor %}
            </ul>
        </div>
        {% endif %}
    </div>
    {% endfor %}

    {% if alerts %}
    <div class="section">
        <h2>Source Alerts</h2>
        <ul>
            {% for alert in alerts %}
            <li>
                <strong>{{ alert.source }}</strong>:
                {{ alert.message }}
                {% if alert.get('latest_expiry') %}
                <span class="source-meta">(latest cookie expiry: {{ alert.latest_expiry }})</span>
                {% endif %}
            </li>
            {% endfor %}
        </ul>
    </div>
    {% endif %}

    {% if team_highlights %}
    <div class="section">
        <h2>Team Notes</h2>
        {% for team, highlight in team_highlights.items() %}
        <h3><span class="team-tag">{{ team }}</span></h3>
        <div>{{ highlight.summary | replace('\\n', '<br>') }}</div>
        {% if highlight.get('sources') %}
        <div class="sources">
            <strong>Sources</strong>
            <ul>
                {% for source in highlight.sources %}
                <li>
                    <a href="{{ source.url }}" target="_blank" rel="noopener noreferrer">{{ source.title }}</a>
                    {% if source.get('source') %}
                    <span class="source-meta">({{ source.source }})</span>
                    {% endif %}
                </li>
                {% endfor %}
            </ul>
        </div>
        {% endif %}
        {% endfor %}
    </div>
    {% endif %}

    {% if yt_section and yt_section.get('transcript_count') %}
    <div class="section">
        <div class="section-header">
            <h2>YouTube Highlights</h2>
            <span class="badge">{{ yt_section.transcript_count }} transcripts</span>
        </div>
        {% if yt_section.get('press_conferences', {}).get('summary') %}
        <h3>Press Conference Highlights</h3>
        <div>{{ yt_section.press_conferences.summary | replace('\\n', '<br>') }}</div>
        {% if yt_section.press_conferences.get('sources') %}
        <div class="sources">
            <strong>Videos</strong>
            <ul>
                {% for s in yt_section.press_conferences.sources %}
                <li><a href="{{ s.url }}" target="_blank" rel="noopener noreferrer">{{ s.team }} — {{ s.title }}</a></li>
                {% endfor %}
            </ul>
        </div>
        {% endif %}
        {% endif %}
        {% if yt_section.get('team_notes') %}
        <h3>Per-Team Notes</h3>
        {% for team, note in yt_section.team_notes.items() %}
        <h3><span class="team-tag">{{ team }}</span></h3>
        <div>{{ note.summary | replace('\\n', '<br>') }}</div>
        {% if note.get('numbered_sources') %}
        <div class="sources">
            <strong>Sources</strong>
            <ul>
                {% for source in note.numbered_sources %}
                <li>
                    <strong>[{{ source.num }}]</strong>
                    <a href="{{ source.url }}" target="_blank" rel="noopener noreferrer">{{ source.title }}</a>
                    {% if source.get('source') %}
                    <span class="source-meta">({{ source.source }})</span>
                    {% endif %}
                </li>
                {% endfor %}
            </ul>
        </div>
        {% endif %}
        {% endfor %}
        {% endif %}
    </div>
    {% endif %}

    <div class="stats">
        <strong>Collection Stats:</strong>
        {% for source, count in collection_stats.items() %}
        {{ source }}: {{ count }}{% if not loop.last %} | {% endif %}
        {% endfor %}
    </div>

    {% if llm_usage %}
    <div class="stats">
        <strong>LLM Usage:</strong>
        {{ llm_usage.get('provider', 'unknown') | upper }} / {{ llm_usage.get('model', 'unknown') }}
        {% if llm_usage.get('pricing_model') == 'mixed' %}
        <span class="source-meta">(mixed models — some sections upgraded)</span>
        {% endif %}
        {% if llm_usage.get('request_count') %}
        | calls: {{ llm_usage.get('request_count') }}
        {% endif %}
        {% if llm_usage.get('input_tokens') %}
        | input: {{ llm_usage.get('input_tokens') }}
        {% endif %}
        {% if llm_usage.get('cached_input_tokens') %}
        | cached input: {{ llm_usage.get('cached_input_tokens') }}
        {% endif %}
        {% if llm_usage.get('output_tokens') %}
        | output: {{ llm_usage.get('output_tokens') }}
        {% endif %}
        {% if llm_usage.get('reasoning_tokens') %}
        | reasoning: {{ llm_usage.get('reasoning_tokens') }}
        {% endif %}
        {% if llm_usage.get('estimated_cost_usd') is not none %}
        | est. cost: ${{ "%.4f"|format(llm_usage.get('estimated_cost_usd', 0.0)) }}
        {% endif %}
        {% if llm_usage.get('tracking_note') %}
        <br>{{ llm_usage.get('tracking_note') }}
        {% endif %}
    </div>
    {% endif %}

    <div class="generated">
        Generated at {{ generated_at }} by NFL News Agent
    </div>
</body>
</html>"""
)

SECTION_TITLES = {
    "transactions": "Transactions & Signings",
    "roster_moves": "Roster Moves",
    "injuries": "Injury Reports",
    "injury_report_changes": "Injury Report Changes",
    "game_day_inactives": "Game-Day Inactives",
    "depth_chart_movement": "Depth Chart Movement",
    "projection_movers": "Today's Projection Movers",
    "projection_audit": "Projection Audit",
    "line_movement": "Line Movement",
    "league_wide": "League-Wide Notes",
    "fantasypoints": "FantasyPoints Player Notes",
}

# Order in which sections render in the report. Keys not listed here
# fall back to dict-insertion order at the tail (covers old reports
# loaded from disk that still have legacy "press_conferences" / "analysis"
# keys — they render at the tail rather than disappearing).
SECTION_ORDER = [
    "transactions",
    "roster_moves",            # in-season only
    "injuries",
    "injury_report_changes",   # in-season only
    "game_day_inactives",      # in-season only, game days
    "depth_chart_movement",
    "projection_movers",
    "line_movement",           # in-season only
    "projection_audit",        # in-season only
    "league_wide",
    "fantasypoints",
]

DEPTH_CHART_TYPE_LABELS = {
    "promoted": "Promotions",
    "demoted": "Demotions",
    "added": "Added",
    "removed": "Removed",
    "team_change": "Team changes",
    "position_change": "Position changes",
    # Emitted only in-season by depth_chart_collector.split_reserve_changes
    "status_change": "Status changes",
}

# In-season roster event grouping (processing/roster_events.py event_type
# -> section label). Order = render order.
ROSTER_EVENT_LABELS = {
    "ir_placed": "Placed on IR",
    "ir_designated_return": "Designated to return",
    "ir_activated": "Activated from IR",
    "pup_placed": "Placed on PUP",
    "pup_activated": "Activated from PUP",
    "nfi_placed": "Placed on NFI",
    "nfi_activated": "Activated from NFI",
    "suspended": "Suspended",
    "reinstated": "Reinstated",
    "exempt": "Exempt list",
    "ps_elevated": "Practice squad elevations",
    "ps_promoted": "Signed to active roster",
    "ps_signed": "Signed to practice squad",
    "ps_released": "Released from practice squad",
    "claimed": "Claimed off waivers",
    "traded": "Trades",
    "signed": "Signings",
    "waived": "Waived",
    "released": "Released",
    "injury_settlement": "Injury settlements",
    "retired": "Retired",
    "team_change": "Team changes",
    "status_change": "Other status changes",
}

AUDIT_SEVERITY_ORDER = ["error", "warning", "info"]
AUDIT_SEVERITY_LABELS = {"error": "Fix before publishing", "warning": "Check", "info": "FYI"}


def _sort_by_published(items: list[Any]) -> list[Any]:
    """Return items ordered newest first."""
    return sorted(items, key=lambda item: item.published, reverse=True)


def _build_news_source(item: NewsItem) -> dict[str, Any]:
    """Build a source payload for a news item."""
    return {
        "title": item.title,
        "url": item.url,
        "source": item.source,
        "source_type": item.source_type,
        "published": item.published.isoformat(),
        "teams": item.teams,
    }


def _dedupe_sources(
    sources: list[dict[str, Any]],
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Drop duplicate sources while preserving order."""
    unique_sources: list[dict[str, Any]] = []
    seen: set[str] = set()

    for source in sources:
        url = str(source.get("url", "")).strip()
        title = str(source.get("title", "")).strip()
        key = url or title
        if not key or key in seen:
            continue

        seen.add(key)
        unique_sources.append(source)

        if limit is not None and len(unique_sources) >= limit:
            break

    return unique_sources


def _normalize_section(section_data: Any) -> dict[str, Any]:
    """Normalize section payloads to a consistent dict shape."""
    if isinstance(section_data, dict):
        normalized = dict(section_data)
        normalized.setdefault("summary", "")
        normalized.setdefault("sources", [])
        return normalized

    return {
        "summary": str(section_data or ""),
        "sources": [],
    }


def _normalize_team_highlight(highlight: Any) -> dict[str, Any]:
    """Normalize team highlight payloads to a consistent dict shape."""
    if isinstance(highlight, dict):
        normalized = dict(highlight)
        normalized.setdefault("summary", "")
        normalized.setdefault("sources", [])
        return normalized

    return {
        "summary": str(highlight or ""),
        "sources": [],
    }


def _build_section_sources(
    news_items: list[NewsItem],
) -> dict[str, list[dict[str, Any]]]:
    """Attach concrete sources for each top-level report section."""
    transactions = _sort_by_published(
        [item for item in news_items if item.category == "transaction"]
    )
    injuries = _sort_by_published(
        [item for item in news_items if item.category == "injury"]
    )
    # Same ordering as summarizer._order_league_wide: real outlets first,
    # then primary sources, then recency — otherwise the flat source list
    # is all tweets while the bullets cite outlets.
    league_wide_items = sorted(
        [
            item for item in news_items
            if not item.teams
            and item.category not in ("transaction", "injury")
        ],
        key=lambda i: (
            0 if i.source_type == "twitter" else 1,
            1 if is_primary_source(i.source) else 0,
            i.published,
        ),
        reverse=True,
    )

    return {
        "transactions": _dedupe_sources(
            [_build_news_source(item) for item in transactions],
            limit=DEFAULT_SECTION_SOURCE_LIMIT,
        ),
        "injuries": _dedupe_sources(
            [_build_news_source(item) for item in injuries],
            limit=DEFAULT_SECTION_SOURCE_LIMIT,
        ),
        "league_wide": _dedupe_sources(
            [_build_news_source(item) for item in league_wide_items],
            limit=LEAGUE_WIDE_SOURCE_LIMIT,
        ),
    }


def _team_source_limit() -> int:
    """Per-team flat source-list size (`team_notes.source_limit`), sized to
    keep pace with the summarizer's `team_notes.item_limit` pool."""
    try:
        return max(1, int(
            (get_settings().get("team_notes", {}) or {})
            .get("source_limit", TEAM_SOURCE_LIMIT)
        ))
    except Exception:
        return TEAM_SOURCE_LIMIT


def _build_team_sources(
    news_items: list[NewsItem],
) -> dict[str, list[dict[str, Any]]]:
    """Build per-team source lists for the news-only Team Notes section."""
    team_items: dict[str, list[NewsItem]] = {}

    for item in news_items:
        if item.category in ("transaction", "injury"):
            continue
        for team in item.teams:
            team_items.setdefault(team, []).append(item)

    team_sources: dict[str, list[dict[str, Any]]] = {}
    source_limit = _team_source_limit()
    for team, items in team_items.items():
        # Round-robin across distinct source labels first so a single
        # high-volume source (e.g. SI team pages, all stamped at scrape
        # time) doesn't crowd out other sources with real published
        # timestamps. Then fill remaining slots by recency.
        by_source: dict[str, list[NewsItem]] = {}
        for it in items:
            by_source.setdefault(it.source or "?", []).append(it)
        for src in by_source:
            by_source[src].sort(key=lambda x: x.published, reverse=True)

        picked: list[NewsItem] = []
        max_per_source = 2
        for r in range(max_per_source):
            for src in list(by_source.keys()):
                bucket = by_source[src]
                if r < len(bucket) and len(picked) < source_limit:
                    picked.append(bucket[r])
        if len(picked) < source_limit:
            remaining = [it for it in _sort_by_published(items) if it not in picked]
            picked.extend(remaining[: source_limit - len(picked)])

        sources = [_build_news_source(item) for item in picked]
        team_sources[team] = _dedupe_sources(sources, limit=source_limit)

    return team_sources


def _build_depth_chart_section(changes: list[dict]) -> dict[str, Any]:
    """Render depth-chart change list as a per-team grouped bullet summary."""
    if not changes:
        return {"summary": "No depth chart changes today.", "count": 0}

    by_team: dict[str, dict[str, list[str]]] = {}
    for change in changes:
        team = change.get("team") or change.get("new_team") or change.get("old_team") or "?"
        ctype = str(change.get("type") or "")
        label = DEPTH_CHART_TYPE_LABELS.get(ctype, ctype.replace("_", " ").title() or "Other")
        message = str(change.get("message") or "").strip()
        if not message:
            continue
        by_team.setdefault(team, {}).setdefault(label, []).append(message)

    parts: list[str] = []
    for team in sorted(by_team.keys()):
        parts.append(f"### {team}")
        seen_labels: set[str] = set()
        # Render in the canonical order first, then any extras the
        # collector produced that aren't in the predefined dict.
        ordered_labels = list(DEPTH_CHART_TYPE_LABELS.values()) + [
            l for l in by_team[team].keys()
            if l not in DEPTH_CHART_TYPE_LABELS.values()
        ]
        for label in ordered_labels:
            if label in seen_labels:
                continue
            seen_labels.add(label)
            bullets = by_team[team].get(label)
            if not bullets:
                continue
            parts.append(f"**{label}**")
            for b in bullets:
                parts.append(f"- {b}")
        parts.append("")

    summary = "\n".join(parts).strip()
    return {"summary": summary, "count": len(changes)}


def _build_roster_moves_section(events: list[dict]) -> dict[str, Any]:
    """Render in-season roster events grouped by event type, then team.

    Reported-only events (insider tweets not yet confirmed by NFL.com /
    nflverse) are tagged so the reader knows to double-check.
    """
    if not events:
        return {"summary": "No roster moves recorded today.", "count": 0}

    by_type: dict[str, list[dict]] = {}
    for ev in events:
        by_type.setdefault(str(ev.get("event_type") or "status_change"), []).append(ev)

    parts: list[str] = []
    ordered_types = list(ROSTER_EVENT_LABELS) + [t for t in by_type if t not in ROSTER_EVENT_LABELS]
    for etype in ordered_types:
        bucket = by_type.get(etype)
        if not bucket:
            continue
        parts.append(f"**{ROSTER_EVENT_LABELS.get(etype, etype.replace('_', ' ').title())}**")
        for ev in sorted(bucket, key=lambda e: (str(e.get("team") or ""), str(e.get("name") or ""))):
            name = ev.get("name") or "?"
            team = ev.get("team") or ""
            pos = ev.get("pos") or ""
            meta = " / ".join(p for p in [team, pos] if p)
            detail = str(ev.get("detail") or "").strip()
            extras: list[str] = []
            if ev.get("from_team") and ev.get("to_team") and ev["from_team"] != ev["to_team"]:
                extras.append(f"{ev['from_team']} -> {ev['to_team']}")
            if ev.get("earliest_return_week"):
                extras.append(f"eligible Wk {ev['earliest_return_week']}")
            if ev.get("elevations_used") is not None and etype == "ps_elevated":
                extras.append(f"elevation {ev['elevations_used']}/3")
            if str(ev.get("confidence") or "") == "reported":
                extras.append("reported, unconfirmed")
            tail = f" - {'; '.join(extras)}" if extras else ""
            src = str(ev.get("source") or "").replace("news:", "")
            src_part = f" _({src})_" if src else ""
            line = f"- **{name}**"
            if meta:
                line += f" ({meta})"
            if detail and detail.lower() != etype.replace("_", " "):
                # OurLads-derived details repeat "Name (TEAM) Active -> IR"; keep just the transition
                if str(name) and detail.startswith(str(name)):
                    detail = detail[len(str(name)):].lstrip(" ").lstrip("(")
                    detail = detail[len(team) + 1:].lstrip() if team and detail.startswith(f"{team})") else detail
                if detail:
                    line += f": {detail}"
            parts.append(line + tail + src_part)
        parts.append("")

    return {"summary": "\n".join(parts).strip(), "count": len(events)}


_INJURY_CHANGE_ORDER = [
    "designation_set", "designation_changed", "practice_downgrade",
    "new_listing", "practice_upgrade", "cleared",
]


def _build_injury_changes_section(changes: list[dict]) -> dict[str, Any]:
    """Render day-over-day injury report changes grouped by team."""
    if not changes:
        return {"summary": "No injury report changes today.", "count": 0}

    by_team: dict[str, list[dict]] = {}
    for c in changes:
        by_team.setdefault(str(c.get("team") or "?"), []).append(c)

    def _rank(c: dict) -> int:
        t = str(c.get("type") or "")
        return _INJURY_CHANGE_ORDER.index(t) if t in _INJURY_CHANGE_ORDER else len(_INJURY_CHANGE_ORDER)

    parts: list[str] = []
    for team in sorted(by_team):
        parts.append(f"### {team}")
        for c in sorted(by_team[team], key=lambda x: (_rank(x), str(x.get("name") or ""))):
            msg = str(c.get("message") or "").strip()
            if not msg:
                name = c.get("name") or "?"
                msg = f"{name}: {c.get('old') or '-'} -> {c.get('new') or '-'}"
            t = str(c.get("type") or "")
            if t in ("designation_set", "designation_changed") and str(c.get("new") or "").upper() in ("OUT", "D"):
                msg = f"**{msg}**"
            parts.append(f"- {msg}")
        parts.append("")
    return {"summary": "\n".join(parts).strip(), "count": len(changes)}


def _build_inactives_section(week_data: dict) -> dict[str, Any]:
    """Render this week's declared inactives grouped by game, then team.

    ``week_data`` is the ``data/inactives/<season>/wk<NN>.json`` payload.
    Skill positions are bolded so the fantasy-relevant scratches stand out.
    """
    games = (week_data or {}).get("games") or {}
    published = [(gid, g) for gid, g in games.items()
                 if any(t.get("inactives") for t in (g.get("teams") or {}).values())]
    if not published:
        return {"summary": "No inactives published yet this week.", "count": 0}

    skill = {"QB", "RB", "FB", "WR", "TE", "K"}
    parts: list[str] = []
    total = 0
    for _gid, g in sorted(published, key=lambda kv: (kv[1].get("date", ""), kv[1].get("short_name", ""))):
        parts.append(f"### {g.get('short_name') or g.get('name', '')}")
        for team in (g.get("away"), g.get("home")):
            t = (g.get("teams") or {}).get(team)
            if not t or not t.get("inactives"):
                continue
            rows = sorted(t["inactives"], key=lambda p: (0 if p.get("pos") in skill else 1, p.get("pos", ""), p.get("name", "")))
            total += len(rows)
            names = ", ".join(
                (f"**{p.get('name')} ({p.get('pos')})**" if p.get("pos") in skill else f"{p.get('name')} ({p.get('pos')})")
                for p in rows
            )
            tag = " _(post-game)_" if t.get("phase") == "postgame" else ""
            parts.append(f"- **{team}**{tag}: {names}")
        parts.append("")
    return {"summary": "\n".join(parts).strip(), "count": total}


def _build_audit_section(alerts: list[dict]) -> dict[str, Any]:
    """Render projection-audit alerts grouped by severity."""
    if not alerts:
        return {"summary": "Projection audit: no issues found.", "count": 0}

    by_sev: dict[str, list[dict]] = {}
    for a in alerts:
        by_sev.setdefault(str(a.get("severity") or "info"), []).append(a)

    parts: list[str] = []
    for sev in AUDIT_SEVERITY_ORDER + [s for s in by_sev if s not in AUDIT_SEVERITY_ORDER]:
        bucket = by_sev.get(sev)
        if not bucket:
            continue
        parts.append(f"**{AUDIT_SEVERITY_LABELS.get(sev, sev.title())}** ({len(bucket)})")
        for a in sorted(bucket, key=lambda x: (str(x.get("team") or ""), str(x.get("player") or ""))):
            msg = str(a.get("message") or "").strip() or f"{a.get('player')}: {a.get('type')}"
            parts.append(f"- {msg}")
        parts.append("")
    return {"summary": "\n".join(parts).strip(), "count": len(alerts)}


def _parse_rank_int(rank_str: Any) -> Optional[int]:
    """Parse the numeric tail of a position-rank string (e.g. 'RB12' -> 12)."""
    if rank_str is None:
        return None
    s = str(rank_str)
    digits = "".join(ch for ch in s if ch.isdigit())
    return int(digits) if digits else None


def _build_projection_movers_section(movers: list[dict]) -> dict[str, Any]:
    """Render fantasy-rank movers as a sorted bullet list."""
    if not movers:
        return {"summary": "No projection rank changes today.", "count": 0}

    def _delta(rec: dict) -> int:
        old_n = _parse_rank_int(rec.get("rank_old"))
        new_n = _parse_rank_int(rec.get("rank_new"))
        if old_n is None or new_n is None:
            return 0
        return abs(old_n - new_n)

    sortable = [
        m for m in movers
        if m.get("rank_old") is not None and m.get("rank_new") is not None
    ]
    sortable.sort(key=_delta, reverse=True)
    selected = sortable[:PROJECTION_MOVER_LIMIT]

    bullets: list[str] = []
    for rec in selected:
        label = rec.get("label") or rec.get("name") or rec.get("key", "?")
        pos = rec.get("pos") or rec.get("position") or ""
        team = rec.get("team") or ""
        old_rank = rec.get("rank_old")
        new_rank = rec.get("rank_new")
        old_n = _parse_rank_int(old_rank)
        new_n = _parse_rank_int(new_rank)
        arrow = "↑" if (old_n is not None and new_n is not None and new_n < old_n) else "↓"
        meta = " / ".join(p for p in [pos, team] if p)
        meta_part = f" ({meta})" if meta else ""
        line = f"- **{label}**{meta_part}: {old_rank} → {new_rank} {arrow}"
        if rec.get("weekly"):
            # In-season records come from the weekly sheet: show the points move too
            try:
                po, pn = float(rec.get("ppr_old")), float(rec.get("ppr_new"))
                line += f" ({po:.1f} → {pn:.1f} pts)"
            except (TypeError, ValueError):
                pass
        bullets.append(line)

    weekly = next((m for m in movers if m.get("weekly")), None)
    intro: list[str] = []
    if weekly:
        wk = weekly.get("week")
        sheet = weekly.get("sheet") or "primary"
        intro.append(f"_Week {wk} weekly sheet ({sheet}): rank and points changes since the previous snapshot,"
                     f" adjusted players only._")
    summary = "\n".join(intro + bullets) if bullets else "No projection rank changes today."
    return {"summary": summary, "count": len(movers)}


def _trim_odds_payload(odds: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Keep the report-sized slice of the odds week file.

    The full payload carries ~1,200 player-stat entries plus a per-game price
    series; reports are retained for 90 days (`storage.reports_to_keep`), so
    only the pull metadata, each game's current/opening line and the typed
    changes are stored. The complete file stays at data/odds/<season>/wkNN.json
    and is what the dashboard reads.
    """
    if not odds:
        return {}
    games = {}
    for key, g in (odds.get("games") or {}).items():
        games[key] = {k: g.get(k) for k in
                      ("away", "home", "kickoff_et", "opened", "current",
                       "sharp", "sheet", "implied")}
    return {
        "season": odds.get("season"),
        "week": odds.get("week"),
        "updated_at": odds.get("updated_at"),
        "pull": odds.get("pull") or {},
        "games": games,
        "changes": list(odds.get("changes") or []),
        "prop_count": len(odds.get("props") or {}),
    }


def _ordered_sections(sections: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Reorder section dict according to SECTION_ORDER, with unknown keys at the tail."""
    ordered: dict[str, dict[str, Any]] = {}
    for key in SECTION_ORDER:
        if key in sections:
            ordered[key] = sections[key]
    for key, value in sections.items():
        if key not in ordered:
            ordered[key] = value
    return ordered


def build_report(
    date_str: str,
    sections: dict,
    team_highlights: dict,
    news_items: list[NewsItem],
    llm_usage: Optional[dict[str, Any]] = None,
    alerts: Optional[list[dict[str, Any]]] = None,
    depth_chart_changes: Optional[list[dict]] = None,
    projection_movers: Optional[list[dict]] = None,
    yt_section: Optional[dict[str, Any]] = None,
    fp_section: Optional[dict[str, Any]] = None,
    roster_events: Optional[list[dict]] = None,
    injury_changes: Optional[list[dict]] = None,
    audit_alerts: Optional[list[dict]] = None,
    season_meta: Optional[dict[str, Any]] = None,
    inactives: Optional[dict[str, Any]] = None,
    line_movement: Optional[dict[str, Any]] = None,
    odds: Optional[dict[str, Any]] = None,
) -> DailyReport:
    """Build a DailyReport from summarized data.

    roster_events / injury_changes / audit_alerts / season_meta are the
    in-season additions (see processing.season). They default to None and
    add nothing when None, so offseason reports are unchanged.

    yt_section is the optional output of `processing.yt_section.build_yt_section`,
    attached only on local runs invoked with `--include-yt-section`.

    fp_section is the optional output of
    `processing.fp_section.build_fp_section`, rendered as a regular section
    via the standard summary+sources renderer.

    line_movement is the optional output of
    `processing.odds_section.build_odds_section` (same shape as fp_section);
    odds is the raw `data/odds/<season>/wkNN.json` payload, trimmed before it
    is stored on the report.
    """
    source_counts: dict[str, int] = {}
    for item in news_items:
        source_counts[item.source_type] = source_counts.get(item.source_type, 0) + 1
    if yt_section:
        source_counts["youtube"] = yt_section.get("transcript_count", 0)

    sections = dict(sections)  # don't mutate caller's dict
    if depth_chart_changes is not None:
        sections["depth_chart_movement"] = _build_depth_chart_section(depth_chart_changes)
    if projection_movers is not None:
        sections["projection_movers"] = _build_projection_movers_section(projection_movers)
    if fp_section:
        sections["fantasypoints"] = fp_section
    if roster_events is not None:
        sections["roster_moves"] = _build_roster_moves_section(roster_events)
    if injury_changes is not None:
        sections["injury_report_changes"] = _build_injury_changes_section(injury_changes)
    if audit_alerts is not None:
        sections["projection_audit"] = _build_audit_section(audit_alerts)
    if inactives is not None:
        sections["game_day_inactives"] = _build_inactives_section(inactives)
    if line_movement:
        sections["line_movement"] = line_movement

    section_sources = _build_section_sources(news_items)
    normalized_sections: dict[str, dict[str, Any]] = {}
    for section_key, section_data in sections.items():
        normalized = _normalize_section(section_data)
        if not normalized.get("sources"):
            normalized["sources"] = section_sources.get(section_key, [])
        normalized_sections[section_key] = normalized

    normalized_sections = _ordered_sections(normalized_sections)

    team_sources = _build_team_sources(news_items)
    normalized_team_highlights: dict[str, dict[str, Any]] = {}
    for team, highlight in team_highlights.items():
        normalized = _normalize_team_highlight(highlight)
        if not normalized.get("sources"):
            normalized["sources"] = team_sources.get(team, [])
        normalized_team_highlights[team] = normalized

    report = DailyReport(
        date=date_str,
        generated_at=datetime.now(timezone.utc).isoformat(),
        sections=normalized_sections,
        team_highlights=normalized_team_highlights,
        collection_stats=source_counts,
        llm_usage=llm_usage or {},
        alerts=alerts or [],
        depth_chart_changes=depth_chart_changes or [],
        projection_movers=projection_movers or [],
        yt_section=yt_section or {},
        roster_events=roster_events or [],
        injury_changes=injury_changes or [],
        audit_alerts=audit_alerts or [],
        season_meta=season_meta or {},
        inactives=inactives or {},
        odds=_trim_odds_payload(odds),
    )

    return report


def save_report(report: DailyReport):
    """Save report as JSON and HTML."""
    reports_dir = get_data_dir("reports")

    json_path = reports_dir / f"{report.date}.json"
    report.to_json(str(json_path))
    logger.info("Saved JSON report: %s", json_path)

    html_path = reports_dir / f"{report.date}.html"
    html = HTML_TEMPLATE.render(
        date=report.date,
        generated_at=report.generated_at,
        sections=report.sections,
        alerts=report.alerts,
        section_titles=SECTION_TITLES,
        team_highlights=report.team_highlights,
        collection_stats=report.collection_stats,
        llm_usage=report.llm_usage,
        yt_section=report.yt_section,
        season_meta=report.season_meta,
        pm_updated_at=report.pm_updated_at,
    )
    html_path.write_text(html, encoding="utf-8")
    logger.info("Saved HTML report: %s", html_path)

    return json_path, html_path


def list_available_reports() -> list[str]:
    """List available daily report dates (YYYY-MM-DD), most recent first.

    Excludes digest files and any other non-date-named JSON in the reports
    directory.
    """
    reports_dir = get_data_dir("reports")
    dates: list[str] = []
    for path in reports_dir.glob("*.json"):
        stem = path.stem
        try:
            datetime.strptime(stem, "%Y-%m-%d")
        except ValueError:
            continue
        dates.append(stem)
    return sorted(dates, reverse=True)


def load_report(date_str: str) -> DailyReport:
    """Load a report by date."""
    reports_dir = get_data_dir("reports")
    path = reports_dir / f"{date_str}.json"
    return DailyReport.from_json(str(path))
