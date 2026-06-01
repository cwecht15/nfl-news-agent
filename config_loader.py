"""Load and validate configuration files."""

import os
from functools import lru_cache
from pathlib import Path
import yaml
from dotenv import load_dotenv

# Project root
PROJECT_ROOT = Path(__file__).parent
CONFIG_DIR = PROJECT_ROOT / "config"
DATA_DIR = PROJECT_ROOT / "data"

# Load .env
load_dotenv(PROJECT_ROOT / ".env")


def load_yaml(filename: str) -> dict:
    """Load a YAML file from the config directory."""
    path = CONFIG_DIR / filename
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


@lru_cache(maxsize=1)
def get_settings() -> dict:
    return load_yaml("settings.yaml")


@lru_cache(maxsize=1)
def get_teams() -> tuple[dict, ...]:
    data = load_yaml("teams.yaml")
    return tuple(data["teams"])


@lru_cache(maxsize=1)
def get_teams_by_abbr() -> dict[str, dict]:
    return {t["abbr"]: t for t in get_teams()}


@lru_cache(maxsize=1)
def get_sources() -> dict:
    return load_yaml("sources.yaml")


def get_rss_feeds() -> list[dict]:
    return get_sources().get("rss_feeds", [])


def get_beat_writers() -> list[dict]:
    return get_sources().get("beat_writers", [])


def get_web_sources() -> list[dict]:
    sources = get_sources().get("web_sources", [])
    return [s for s in sources if s.get("enabled", True)]


def get_youtube_keywords() -> list[str]:
    return get_sources().get("youtube_keywords", [])


def get_national_youtube_channels() -> list[dict]:
    """Channels with no single-team focus (e.g. NFL on NBC, ESPN).

    Collected by collect_youtube and tagged team="NFL" so they render as a
    league-wide group in the YouTube Report.
    """
    return get_sources().get("national_youtube_channels", [])


def _get_required_env_key(env_name: str) -> str:
    """Load a required API key from the environment."""
    key = os.environ.get(env_name, "")
    if not key or key == "your-api-key-here":
        raise ValueError(
            f"{env_name} not set. Add it to .env file."
        )
    return key


def get_summary_provider() -> str:
    """Return the configured summarization provider."""
    provider = get_settings().get("summarization", {}).get(
        "provider", "anthropic"
    )
    provider = str(provider).strip().lower()
    if provider not in {"anthropic", "openai", "ollama"}:
        raise ValueError(
            f"Unsupported summarization provider: {provider}"
        )
    return provider


def get_anthropic_api_key() -> str:
    return _get_required_env_key("ANTHROPIC_API_KEY")


def get_openai_api_key() -> str:
    return _get_required_env_key("OPENAI_API_KEY")


def get_fantasypoints_auth() -> str:
    return _get_required_env_key("FANTASYPOINTS_AUTH")


def get_fantasypoints_source() -> str:
    return _get_required_env_key("FANTASYPOINTS_SOURCE")


def get_athletic_cookies_path() -> Path:
    """Return the configured Athletic cookies file path."""
    configured = os.environ.get("ATHLETIC_COOKIES_PATH", "").strip()
    candidates = []

    if configured:
        candidates.append(Path(configured))

    candidates.extend([
        PROJECT_ROOT / "secrets" / "athletic_cookies.txt",
        PROJECT_ROOT / "athletic_cookies.txt",
    ])

    for path in candidates:
        expanded = path.expanduser()
        if expanded.exists():
            return expanded

    raise ValueError(
        "Athletic cookies file not found. Set ATHLETIC_COOKIES_PATH in .env "
        "or place athletic_cookies.txt in /secrets."
    )


def get_data_dir(subdir: str, date_str: str = "") -> Path:
    """Get a data subdirectory, creating it if needed."""
    path = DATA_DIR / subdir
    if date_str:
        path = path / date_str
    path.mkdir(parents=True, exist_ok=True)
    return path
