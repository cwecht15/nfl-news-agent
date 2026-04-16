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

from config_loader import get_data_dir
from models import DailyReport, NewsItem, Transcript
from processing.summarizer import _select_press_transcripts

logger = logging.getLogger(__name__)

DEFAULT_SECTION_SOURCE_LIMIT = 8
PRESS_SECTION_SOURCE_LIMIT = 12
TEAM_SOURCE_LIMIT = 6
ANALYSIS_NEWS_SOURCE_LIMIT = 6
ANALYSIS_PRESS_SOURCE_LIMIT = 4

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
        <h2>Team Highlights</h2>
        {% for team, highlight in team_highlights.items() %}
        <h3><span class="team-tag">{{ team }}</span></h3>
        <p>{{ highlight.summary }}</p>
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
    "injuries": "Injury Reports",
    "press_conferences": "Press Conference Highlights",
    "analysis": "Analysis & What to Watch",
}


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


def _build_transcript_source(transcript: Transcript) -> dict[str, Any]:
    """Build a source payload for a transcript."""
    return {
        "title": transcript.title,
        "url": transcript.url,
        "source": transcript.channel_name,
        "source_type": "youtube",
        "published": transcript.published.isoformat(),
        "team": transcript.team,
        "teams": [transcript.team],
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
    transcripts: list[Transcript],
) -> dict[str, list[dict[str, Any]]]:
    """Attach concrete sources for each top-level report section."""
    transactions = _sort_by_published(
        [item for item in news_items if item.category == "transaction"]
    )
    injuries = _sort_by_published(
        [item for item in news_items if item.category == "injury"]
    )
    press_transcripts = _select_press_transcripts(transcripts)
    analysis_news = _sort_by_published(news_items)[:ANALYSIS_NEWS_SOURCE_LIMIT]
    analysis_press = press_transcripts[:ANALYSIS_PRESS_SOURCE_LIMIT]

    return {
        "transactions": _dedupe_sources(
            [_build_news_source(item) for item in transactions],
            limit=DEFAULT_SECTION_SOURCE_LIMIT,
        ),
        "injuries": _dedupe_sources(
            [_build_news_source(item) for item in injuries],
            limit=DEFAULT_SECTION_SOURCE_LIMIT,
        ),
        "press_conferences": _dedupe_sources(
            [_build_transcript_source(transcript) for transcript in press_transcripts],
            limit=PRESS_SECTION_SOURCE_LIMIT,
        ),
        "analysis": _dedupe_sources(
            [_build_news_source(item) for item in analysis_news]
            + [
                _build_transcript_source(transcript)
                for transcript in analysis_press
            ],
            limit=DEFAULT_SECTION_SOURCE_LIMIT,
        ),
    }


def _build_team_sources(
    news_items: list[NewsItem],
    transcripts: list[Transcript],
) -> dict[str, list[dict[str, Any]]]:
    """Build per-team source lists for team highlights."""
    team_items: dict[str, list[Any]] = {}

    for item in news_items:
        for team in item.teams:
            team_items.setdefault(team, []).append(item)

    for transcript in _select_press_transcripts(transcripts):
        team_items.setdefault(transcript.team, []).append(transcript)

    team_sources: dict[str, list[dict[str, Any]]] = {}
    for team, items in team_items.items():
        ordered_items = _sort_by_published(items)
        sources: list[dict[str, Any]] = []

        for item in ordered_items:
            if isinstance(item, NewsItem):
                sources.append(_build_news_source(item))
            else:
                sources.append(_build_transcript_source(item))

        team_sources[team] = _dedupe_sources(sources, limit=TEAM_SOURCE_LIMIT)

    return team_sources


def build_report(
    date_str: str,
    sections: dict,
    team_highlights: dict,
    news_items: list[NewsItem],
    transcripts: list[Transcript],
    llm_usage: Optional[dict[str, Any]] = None,
    alerts: Optional[list[dict[str, Any]]] = None,
) -> DailyReport:
    """Build a DailyReport from summarized data."""
    source_counts: dict[str, int] = {}
    for item in news_items:
        source_counts[item.source_type] = source_counts.get(item.source_type, 0) + 1
    source_counts["youtube"] = len(transcripts)

    section_sources = _build_section_sources(news_items, transcripts)
    normalized_sections: dict[str, dict[str, Any]] = {}
    for section_key, section_data in sections.items():
        normalized = _normalize_section(section_data)
        if not normalized.get("sources"):
            normalized["sources"] = section_sources.get(section_key, [])
        normalized_sections[section_key] = normalized

    team_sources = _build_team_sources(news_items, transcripts)
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
    )
    html_path.write_text(html, encoding="utf-8")
    logger.info("Saved HTML report: %s", html_path)

    return json_path, html_path


def list_available_reports() -> list[str]:
    """List available report dates (YYYY-MM-DD), most recent first."""
    reports_dir = get_data_dir("reports")
    dates = sorted([path.stem for path in reports_dir.glob("*.json")], reverse=True)
    return dates


def load_report(date_str: str) -> DailyReport:
    """Load a report by date."""
    reports_dir = get_data_dir("reports")
    path = reports_dir / f"{date_str}.json"
    return DailyReport.from_json(str(path))
