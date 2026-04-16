"""Shared data models used across all modules."""

from dataclasses import MISSING, dataclass, field, fields, asdict
from datetime import datetime
from typing import Optional
import json


@dataclass
class NewsItem:
    """A single news item from any source."""
    title: str
    url: str
    source: str              # e.g., "ESPN NFL", "NFL.com Transactions"
    source_type: str         # "rss", "web", "twitter", "youtube"
    published: datetime
    summary: str = ""
    full_text: str = ""
    teams: list[str] = field(default_factory=list)       # Team abbreviations
    author: str = ""
    category: str = ""       # "transaction", "injury", "press_conference", "news"
    ai_summary: str = ""     # Filled in by summarizer

    def to_dict(self) -> dict:
        d = asdict(self)
        d["published"] = self.published.isoformat()
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "NewsItem":
        d = d.copy()
        d["published"] = datetime.fromisoformat(d["published"])
        return cls(**d)


@dataclass
class Transcript:
    """A press conference transcript from YouTube."""
    video_id: str
    title: str
    team: str                # Team abbreviation
    channel_name: str
    published: datetime
    url: str
    text: str                # Full transcript text
    method: str              # "captions" or "whisper"
    duration_seconds: int = 0
    ai_summary: str = ""     # Filled in by summarizer

    def to_dict(self) -> dict:
        d = asdict(self)
        d["published"] = self.published.isoformat()
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Transcript":
        d = d.copy()
        d["published"] = datetime.fromisoformat(d["published"])
        return cls(**d)


@dataclass
class DailyReport:
    """The assembled daily report."""
    date: str                # YYYY-MM-DD
    generated_at: str        # ISO timestamp
    sections: dict = field(default_factory=dict)
    team_highlights: dict = field(default_factory=dict)
    collection_stats: dict = field(default_factory=dict)
    llm_usage: dict = field(default_factory=dict)
    alerts: list[dict] = field(default_factory=list)

    def to_json(self, path: str):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(asdict(self), f, indent=2, ensure_ascii=False)

    @classmethod
    def from_json(cls, path: str) -> "DailyReport":
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)

        field_defs = {field_def.name: field_def for field_def in fields(cls)}
        normalized = {}

        for name, field_def in field_defs.items():
            if name in payload:
                normalized[name] = payload[name]
            elif field_def.default is not MISSING:
                normalized[name] = field_def.default
            elif field_def.default_factory is not MISSING:
                normalized[name] = field_def.default_factory()

        return cls(**normalized)
