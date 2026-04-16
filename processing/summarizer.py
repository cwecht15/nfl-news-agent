"""Configurable LLM summarizer.

Supports three providers for the daily briefing pipeline:
1. Anthropic Claude
2. OpenAI Responses API
3. Ollama local API
"""

import logging
import re
import sys
import time
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

import requests

from config_loader import (
    get_anthropic_api_key,
    get_openai_api_key,
    get_settings,
    get_summary_provider,
)
from models import NewsItem, Transcript

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are an NFL news analyst writing a daily briefing for a knowledgeable football fan.
Your style is specific, informative, and insightful. Focus on:
- What happened (facts first)
- Why it matters (impact on team, division, fantasy)
- What to watch next

Use clear structure. Bullets are fine for quick briefings, but when the prompt asks for deeper analysis or team synthesis, use richer bullets or short paragraphs under headings.
Include player names, teams, coaches, executives, and relevant context when available.
Do NOT use filler phrases like "In other news" or "Moving on to". Get straight to the substance.
Use only the information provided in the prompt. If a detail is missing, say it is not specified.
Do not invent dates, player names, contract terms, injuries, quotes, or outcomes."""

PRESS_MAX_ITEMS = 12
ANALYSIS_NEWS_LIMIT = 45
TEAM_HIGHLIGHT_ITEM_LIMIT = 8

PRESS_POSITIVE_SIGNALS = {
    "press conference": 5,
    "media availability": 4,
    "speaks to the media": 4,
    "speaks to media": 4,
    "meets with the media": 4,
    "introductory press": 4,
    "podium": 3,
    "q&a": 2,
}

PRESS_NEGATIVE_SIGNALS = {
    "podcast": -5,
    "breaks down": -5,
    "film": -4,
    "exclusive interview": -4,
    "joins the show": -4,
    "drive time": -3,
    "hq": -3,
    "1-on-1": -3,
    "one-on-one": -3,
}

OPENAI_PRICING = {
    "gpt-5.4": {
        "default": {
            "input": 2.50,
            "cached_input": 0.25,
            "output": 15.00,
        },
        "priority": {
            "input": 5.00,
            "cached_input": 0.50,
            "output": 30.00,
        },
    },
    "gpt-5.4-mini": {
        "default": {
            "input": 0.75,
            "cached_input": 0.075,
            "output": 4.50,
        },
        "priority": {
            "input": 1.50,
            "cached_input": 0.15,
            "output": 9.00,
        },
    },
    "gpt-5.4-nano": {
        "default": {
            "input": 0.20,
            "cached_input": 0.02,
            "output": 1.25,
        },
    },
}


def _get_runtime_config() -> dict[str, Any]:
    """Resolve summarizer provider settings with backward compatibility."""
    settings = get_settings()
    provider = get_summary_provider()
    summary_settings = settings.get("summarization", {})

    max_output_tokens = summary_settings.get("max_output_tokens")
    if max_output_tokens is None:
        max_output_tokens = settings.get("claude", {}).get(
            "max_tokens_per_call", 4096
        )

    if provider == "anthropic":
        anthropic_settings = settings.get("anthropic") or settings.get(
            "claude", {}
        )
        return {
            "provider": provider,
            "model": anthropic_settings.get(
                "model", "claude-sonnet-4-20250514"
            ),
            "max_output_tokens": max_output_tokens,
            "use_prompt_caching": anthropic_settings.get(
                "use_prompt_caching",
                settings.get("claude", {}).get("use_prompt_caching", True),
            ),
        }

    if provider == "openai":
        openai_settings = settings.get("openai", {})
        return {
            "provider": provider,
            "model": openai_settings.get("model", "gpt-5.4-mini"),
            "max_output_tokens": max_output_tokens,
            "reasoning_effort": openai_settings.get("reasoning_effort", "low"),
            "service_tier": openai_settings.get("service_tier", "default"),
        }

    ollama_settings = settings.get("ollama", {})
    base_url = str(
        ollama_settings.get("base_url", "http://localhost:11434")
    ).rstrip("/")
    return {
        "provider": provider,
        "model": ollama_settings.get("model", "gemma3:4b"),
        "max_output_tokens": max_output_tokens,
        "base_url": base_url,
        "timeout_seconds": ollama_settings.get("timeout_seconds", 180),
        "temperature": ollama_settings.get("temperature", 0.2),
    }


def _get_client(provider: str) -> Any:
    """Create an API client for the configured provider."""
    if provider == "anthropic":
        try:
            import anthropic
        except ImportError as exc:
            raise ValueError(
                "Anthropic SDK not installed. Install with: pip install anthropic"
            ) from exc
        return anthropic.Anthropic(api_key=get_anthropic_api_key())

    if provider == "openai":
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise ValueError(
                "OpenAI SDK not installed. Install with: pip install openai"
            ) from exc
        return OpenAI(api_key=get_openai_api_key())

    if provider == "ollama":
        session = requests.Session()
        session.headers.update({"Content-Type": "application/json"})
        return session

    raise ValueError(f"Unsupported summarization provider: {provider}")


def _obj_get(obj: Any, key: str, default: Any = None) -> Any:
    """Read a field from a dict-like or object response."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _obj_int(obj: Any, key: str) -> int:
    """Read an integer field from a dict-like or object response."""
    value = _obj_get(obj, key, 0)
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _normalize_service_tier(tier: Optional[str]) -> str:
    """Normalize the service tier name for pricing lookups."""
    normalized = str(tier or "default").strip().lower()
    if normalized in {"", "auto", "default", "standard"}:
        return "default"
    return normalized


def _resolve_openai_pricing(model: str, service_tier: str) -> tuple[str, str, dict[str, float]]:
    """Resolve OpenAI pricing for the configured model and tier."""
    normalized_tier = _normalize_service_tier(service_tier)
    pricing_key = model
    if pricing_key not in OPENAI_PRICING:
        for candidate in OPENAI_PRICING:
            if model.startswith(candidate):
                pricing_key = candidate
                break

    pricing = OPENAI_PRICING.get(pricing_key, OPENAI_PRICING["gpt-5.4"])
    if normalized_tier not in pricing:
        normalized_tier = "default"

    return pricing_key, normalized_tier, pricing[normalized_tier]


def _init_usage_tracker(runtime: dict[str, Any]) -> dict[str, Any]:
    """Initialize a per-run usage tracker."""
    tracker = {
        "provider": runtime["provider"],
        "model": runtime["model"],
        "service_tier": runtime.get("service_tier", "default"),
        "tracked": runtime["provider"] in ("openai", "anthropic"),
        "request_count": 0,
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "uncached_input_tokens": 0,
        "output_tokens": 0,
        "reasoning_tokens": 0,
        "total_tokens": 0,
        "input_cost_usd": 0.0,
        "output_cost_usd": 0.0,
        "estimated_cost_usd": 0.0,
    }

    if runtime["provider"] == "openai":
        tracker["tracking_note"] = (
            "Exact token usage comes from OpenAI Responses API metadata."
        )
    elif runtime["provider"] == "anthropic":
        tracker["tracking_note"] = (
            "Exact token usage comes from Anthropic Messages API metadata."
        )
    elif runtime["provider"] == "ollama":
        tracker["tracking_note"] = (
            "Ollama runs locally, so API cost does not apply."
        )

    return tracker


def _record_openai_usage(
    response: Any,
    runtime: dict[str, Any],
    usage_tracker: Optional[dict[str, Any]],
    usage_label: Optional[str] = None,
):
    """Accumulate token usage and estimated cost from an OpenAI response."""
    if usage_tracker is None:
        return

    usage = _obj_get(response, "usage")
    if usage is None:
        return

    input_tokens = _obj_int(usage, "input_tokens")
    input_details = _obj_get(usage, "input_tokens_details")
    cached_input_tokens = _obj_int(input_details, "cached_tokens")
    uncached_input_tokens = max(input_tokens - cached_input_tokens, 0)
    output_tokens = _obj_int(usage, "output_tokens")
    output_details = _obj_get(usage, "output_tokens_details")
    reasoning_tokens = _obj_int(output_details, "reasoning_tokens")
    total_tokens = _obj_int(usage, "total_tokens")

    actual_tier = _normalize_service_tier(
        _obj_get(response, "service_tier", runtime.get("service_tier", "default"))
    )
    pricing_model, pricing_tier, pricing = _resolve_openai_pricing(
        runtime["model"],
        actual_tier,
    )

    input_cost = (
        (uncached_input_tokens / 1_000_000) * pricing["input"]
        + (cached_input_tokens / 1_000_000) * pricing["cached_input"]
    )
    output_cost = (output_tokens / 1_000_000) * pricing["output"]

    usage_tracker["service_tier"] = pricing_tier
    usage_tracker["pricing_model"] = pricing_model
    usage_tracker["request_count"] += 1
    usage_tracker["input_tokens"] += input_tokens
    usage_tracker["cached_input_tokens"] += cached_input_tokens
    usage_tracker["uncached_input_tokens"] += uncached_input_tokens
    usage_tracker["output_tokens"] += output_tokens
    usage_tracker["reasoning_tokens"] += reasoning_tokens
    usage_tracker["total_tokens"] += total_tokens
    usage_tracker["input_cost_usd"] += input_cost
    usage_tracker["output_cost_usd"] += output_cost
    usage_tracker["estimated_cost_usd"] += input_cost + output_cost

    if usage_label:
        usage_tracker.setdefault("operations", []).append({
            "name": usage_label,
            "input_tokens": input_tokens,
            "cached_input_tokens": cached_input_tokens,
            "uncached_input_tokens": uncached_input_tokens,
            "output_tokens": output_tokens,
            "reasoning_tokens": reasoning_tokens,
            "total_tokens": total_tokens,
            "estimated_cost_usd": round(input_cost + output_cost, 6),
        })


ANTHROPIC_PRICING = {
    "claude-sonnet-4-20250514": {"input": 3.00, "output": 15.00, "cache_write": 3.75, "cache_read": 0.30},
    "claude-opus-4-20250514": {"input": 15.00, "output": 75.00, "cache_write": 18.75, "cache_read": 1.50},
    "claude-haiku-4-20250506": {"input": 0.80, "output": 4.00, "cache_write": 1.00, "cache_read": 0.08},
}


def _resolve_anthropic_pricing(model: str) -> dict[str, float]:
    """Resolve Anthropic pricing for the configured model."""
    if model in ANTHROPIC_PRICING:
        return ANTHROPIC_PRICING[model]
    for candidate, pricing in ANTHROPIC_PRICING.items():
        if model.startswith(candidate.rsplit("-", 1)[0]):
            return pricing
    return ANTHROPIC_PRICING.get("claude-sonnet-4-20250514", {"input": 3.00, "output": 15.00, "cache_write": 3.75, "cache_read": 0.30})


def _record_anthropic_usage(
    response: Any,
    runtime: dict[str, Any],
    usage_tracker: Optional[dict[str, Any]],
    usage_label: Optional[str] = None,
):
    """Accumulate token usage and estimated cost from an Anthropic response."""
    if usage_tracker is None:
        return

    usage = getattr(response, "usage", None)
    if usage is None:
        return

    input_tokens = getattr(usage, "input_tokens", 0) or 0
    output_tokens = getattr(usage, "output_tokens", 0) or 0
    cache_creation = getattr(usage, "cache_creation_input_tokens", 0) or 0
    cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
    uncached_input = max(input_tokens - cache_read, 0)

    pricing = _resolve_anthropic_pricing(runtime["model"])
    input_cost = (
        (uncached_input / 1_000_000) * pricing["input"]
        + (cache_creation / 1_000_000) * pricing["cache_write"]
        + (cache_read / 1_000_000) * pricing["cache_read"]
    )
    output_cost = (output_tokens / 1_000_000) * pricing["output"]

    usage_tracker["request_count"] += 1
    usage_tracker["input_tokens"] += input_tokens
    usage_tracker["cached_input_tokens"] += cache_read
    usage_tracker["uncached_input_tokens"] += uncached_input
    usage_tracker["output_tokens"] += output_tokens
    usage_tracker["total_tokens"] += input_tokens + output_tokens
    usage_tracker["input_cost_usd"] += input_cost
    usage_tracker["output_cost_usd"] += output_cost
    usage_tracker["estimated_cost_usd"] += input_cost + output_cost

    # Track cache creation tokens separately
    usage_tracker.setdefault("cache_creation_tokens", 0)
    usage_tracker["cache_creation_tokens"] += cache_creation

    if usage_label:
        usage_tracker.setdefault("operations", []).append({
            "name": usage_label,
            "input_tokens": input_tokens,
            "cached_input_tokens": cache_read,
            "cache_creation_tokens": cache_creation,
            "uncached_input_tokens": uncached_input,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
            "estimated_cost_usd": round(input_cost + output_cost, 6),
        })


def _extract_openai_text(response: Any) -> str:
    """Extract visible text from an OpenAI Responses API payload."""
    text = str(getattr(response, "output_text", "") or "").strip()
    if text:
        return text

    text_blocks = []
    for output_item in _obj_get(response, "output", []) or []:
        content_items = _obj_get(output_item, "content", []) or []
        for content_item in content_items:
            content_type = str(_obj_get(content_item, "type", "") or "").lower()
            if content_type not in {"output_text", "text"}:
                continue

            raw_text = _obj_get(content_item, "text", "")
            if isinstance(raw_text, str):
                cleaned = raw_text.strip()
            else:
                cleaned = str(_obj_get(raw_text, "value", "") or "").strip()

            if cleaned:
                text_blocks.append(cleaned)

    return "\n".join(text_blocks).strip()


def _should_retry_openai_no_text(response: Any, text: str) -> bool:
    """Detect cases where GPT spent the output budget on reasoning only."""
    if text:
        return False

    usage = _obj_get(response, "usage")
    output_tokens = _obj_int(usage, "output_tokens")
    output_details = _obj_get(usage, "output_tokens_details")
    reasoning_tokens = _obj_int(output_details, "reasoning_tokens")
    status = str(_obj_get(response, "status", "") or "").lower()
    incomplete_details = _obj_get(response, "incomplete_details")

    return (
        status == "incomplete"
        or incomplete_details is not None
        or (output_tokens > 0 and output_tokens == reasoning_tokens)
    )


def _get_openai_retry_max_tokens(max_tokens: int) -> Optional[int]:
    """Compute a larger retry budget for high-reasoning no-text responses."""
    retry_tokens = min(max(max_tokens * 2, max_tokens + 1024, 1200), 8192)
    if retry_tokens <= max_tokens:
        return None
    return retry_tokens


def _build_openai_no_text_message(response: Any) -> str:
    """Build a more helpful fallback message when OpenAI returns no visible text."""
    incomplete_details = _obj_get(response, "incomplete_details")
    incomplete_reason = _obj_get(incomplete_details, "reason")
    status = str(_obj_get(response, "status", "") or "").strip()

    if incomplete_reason:
        return f"[Summary unavailable: OpenAI returned no text output ({incomplete_reason})]"
    if status and status != "completed":
        return f"[Summary unavailable: OpenAI returned no text output ({status})]"
    return "[Summary unavailable: OpenAI returned no text output]"


MAX_RATE_LIMIT_RETRIES = 3


def _call_anthropic(
    client: Any,
    user_prompt: str,
    runtime: dict[str, Any],
    max_tokens: int,
    usage_tracker: Optional[dict[str, Any]] = None,
    usage_label: Optional[str] = None,
    _retry_count: int = 0,
) -> str:
    """Make a single Anthropic API call."""
    try:
        import anthropic
    except ImportError as exc:
        raise ValueError(
            "Anthropic SDK not installed. Install with: pip install anthropic"
        ) from exc

    system_block = {
        "type": "text",
        "text": SYSTEM_PROMPT,
    }
    if runtime.get("use_prompt_caching", True):
        system_block["cache_control"] = {"type": "ephemeral"}

    try:
        response = client.messages.create(
            model=runtime["model"],
            max_tokens=max_tokens,
            system=[system_block],
            messages=[{"role": "user", "content": user_prompt}],
        )
        _record_anthropic_usage(response, runtime, usage_tracker, usage_label=usage_label)
        text_blocks = [
            block.text
            for block in getattr(response, "content", [])
            if getattr(block, "type", "") == "text"
            and getattr(block, "text", "")
        ]
        if text_blocks:
            return "\n".join(text_blocks).strip()
        return "[Summary unavailable: Anthropic returned no text output]"
    except anthropic.RateLimitError:
        if _retry_count >= MAX_RATE_LIMIT_RETRIES:
            logger.error("Anthropic rate limited %d times, giving up.", _retry_count)
            return "[Summary unavailable: rate limited]"
        logger.warning("Anthropic rate limited - waiting 60s before retry (%d/%d)...", _retry_count + 1, MAX_RATE_LIMIT_RETRIES)
        time.sleep(60)
        return _call_anthropic(
            client,
            user_prompt,
            runtime,
            max_tokens,
            usage_tracker=usage_tracker,
            usage_label=usage_label,
            _retry_count=_retry_count + 1,
        )
    except Exception as e:
        logger.error("Anthropic API error: %s", e)
        return f"[Summary unavailable: {e}]"


def _call_openai(
    client: Any,
    user_prompt: str,
    runtime: dict[str, Any],
    max_tokens: int,
    usage_tracker: Optional[dict[str, Any]] = None,
    usage_label: Optional[str] = None,
    verbosity: str = "low",
    reasoning_effort: Optional[str] = None,
    _retry_count: int = 0,
) -> str:
    """Make a single OpenAI Responses API call."""
    try:
        from openai import RateLimitError
    except ImportError as exc:
        raise ValueError(
            "OpenAI SDK not installed. Install with: pip install openai"
        ) from exc

    def _create_response(request_max_tokens: int) -> Any:
        resolved_reasoning_effort = reasoning_effort or runtime.get(
            "reasoning_effort", "low"
        )
        return client.responses.create(
            model=runtime["model"],
            instructions=SYSTEM_PROMPT,
            input=user_prompt,
            max_output_tokens=request_max_tokens,
            reasoning={"effort": resolved_reasoning_effort},
            text={"verbosity": verbosity},
            service_tier=runtime.get("service_tier", "default"),
        )

    try:
        response = _create_response(max_tokens)
        _record_openai_usage(
            response,
            runtime,
            usage_tracker,
            usage_label=usage_label,
        )
        text = _extract_openai_text(response)
        if text:
            return text

        if _should_retry_openai_no_text(response, text):
            retry_max_tokens = _get_openai_retry_max_tokens(max_tokens)
            if retry_max_tokens:
                logger.warning(
                    (
                        "OpenAI returned no visible text for %s at max_output_tokens=%d; "
                        "retrying once with max_output_tokens=%d"
                    ),
                    usage_label or "summary",
                    max_tokens,
                    retry_max_tokens,
                )
                retry_response = _create_response(retry_max_tokens)
                _record_openai_usage(
                    retry_response,
                    runtime,
                    usage_tracker,
                    usage_label=f"{usage_label}:retry" if usage_label else "retry",
                )
                retry_text = _extract_openai_text(retry_response)
                if retry_text:
                    return retry_text
                response = retry_response

        return _build_openai_no_text_message(response)
    except RateLimitError:
        if _retry_count >= MAX_RATE_LIMIT_RETRIES:
            logger.error("OpenAI rate limited %d times, giving up.", _retry_count)
            return "[Summary unavailable: rate limited]"
        logger.warning("OpenAI rate limited - waiting 60s before retry (%d/%d)...", _retry_count + 1, MAX_RATE_LIMIT_RETRIES)
        time.sleep(60)
        return _call_openai(
            client,
            user_prompt,
            runtime,
            max_tokens,
            usage_tracker=usage_tracker,
            usage_label=usage_label,
            verbosity=verbosity,
            reasoning_effort=reasoning_effort,
            _retry_count=_retry_count + 1,
        )
    except Exception as e:
        logger.error("OpenAI API error: %s", e)
        return f"[Summary unavailable: {e}]"


def _call_ollama(
    client: requests.Session,
    user_prompt: str,
    runtime: dict[str, Any],
    max_tokens: int,
    usage_tracker: Optional[dict[str, Any]] = None,
    usage_label: Optional[str] = None,
) -> str:
    """Make a single Ollama API call."""
    url = f"{runtime['base_url']}/api/generate"
    payload = {
        "model": runtime["model"],
        "system": SYSTEM_PROMPT,
        "prompt": user_prompt,
        "stream": False,
        "options": {
            "temperature": runtime.get("temperature", 0.2),
            "num_predict": max_tokens,
        },
    }

    try:
        response = client.post(
            url,
            json=payload,
            timeout=runtime.get("timeout_seconds", 180),
        )
        response.raise_for_status()
        data = response.json()
        text = str(data.get("response", "") or "").strip()
        if text:
            return text
        return "[Summary unavailable: Ollama returned no text output]"
    except requests.RequestException as e:
        logger.error("Ollama API error: %s", e)
        return f"[Summary unavailable: {e}]"


def _check_token_budget(usage_tracker: Optional[dict[str, Any]], label: str) -> Optional[str]:
    """Return a fallback message if the daily token budget has been exceeded."""
    if usage_tracker is None:
        return None
    budget = get_settings().get("summarization", {}).get("daily_token_budget")
    if budget is None:
        return None
    total = usage_tracker.get("input_tokens", 0) + usage_tracker.get("output_tokens", 0)
    if total >= budget:
        logger.warning(
            "Token budget exceeded (%d/%d) — skipping %s",
            total, budget, label or "call",
        )
        return f"[Summary skipped: daily token budget ({budget:,}) exceeded]"
    return None


def _call_model(
    client: Any,
    user_prompt: str,
    runtime: dict[str, Any],
    max_tokens: Optional[int] = None,
    usage_tracker: Optional[dict[str, Any]] = None,
    usage_label: Optional[str] = None,
    verbosity: str = "low",
    reasoning_effort: Optional[str] = None,
) -> str:
    """Route a prompt to the configured provider."""
    budget_msg = _check_token_budget(usage_tracker, usage_label)
    if budget_msg:
        return budget_msg

    if max_tokens is None:
        max_tokens = runtime["max_output_tokens"]

    if runtime["provider"] == "openai":
        return _call_openai(
            client,
            user_prompt,
            runtime,
            max_tokens,
            usage_tracker=usage_tracker,
            usage_label=usage_label,
            verbosity=verbosity,
            reasoning_effort=reasoning_effort,
        )
    if runtime["provider"] == "ollama":
        return _call_ollama(
            client,
            user_prompt,
            runtime,
            max_tokens,
            usage_tracker=usage_tracker,
            usage_label=usage_label,
        )
    return _call_anthropic(
        client,
        user_prompt,
        runtime,
        max_tokens,
        usage_tracker=usage_tracker,
        usage_label=usage_label,
    )


def _resolve_client_and_runtime(
    client: Optional[Any] = None,
) -> tuple[Any, dict[str, Any]]:
    """Reuse an existing client or create one for the active provider."""
    runtime = _get_runtime_config()
    if client is None:
        client = _get_client(runtime["provider"])
    return client, runtime


def _press_relevance_score(title: str) -> int:
    """Score a transcript title for press-conference relevance."""
    title_lower = title.lower()
    score = 0

    for phrase, weight in PRESS_POSITIVE_SIGNALS.items():
        if phrase in title_lower:
            score += weight

    for phrase, weight in PRESS_NEGATIVE_SIGNALS.items():
        if phrase in title_lower:
            score += weight

    if "press conference" in title_lower and "postgame" in title_lower:
        score += 1
    if re.search(r"\bspeaks?\b", title_lower) and "media" in title_lower:
        score += 1

    return score


def _select_press_transcripts(
    transcripts: list[Transcript],
    limit: Optional[int] = None,
) -> list[Transcript]:
    """Keep only the most press-conference-like transcripts."""
    scored = []
    for transcript in transcripts:
        score = _press_relevance_score(transcript.title)
        if score <= 0:
            continue
        scored.append((score, transcript.published.timestamp(), transcript))

    scored.sort(key=lambda row: (row[0], row[1]), reverse=True)
    selected = [transcript for _, _, transcript in scored]
    if limit is not None:
        selected = selected[:limit]
    return selected


def _normalize_bullet_summary(text: str, max_bullets: int = 2) -> str:
    """Normalize model output into a short bullet list."""
    cleaned = text.replace("•", "-")
    bullets = []

    for raw_line in cleaned.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        line = re.sub(
            r"^(?:[-*]\s*)?(?:\*\*)?bullet\s*\d+\s*:\s*(?:\*\*)?",
            "",
            line,
            flags=re.IGNORECASE,
        ).strip()
        line = re.sub(r"^(?:[-*•]\s*)+", "", line).strip()

        if not line:
            continue

        bullets.append(f"- {line}")
        if len(bullets) >= max_bullets:
            break

    if bullets:
        return "\n".join(bullets)

    fallback = cleaned.strip()
    if not fallback:
        return "- No clear press-conference takeaway extracted."
    return f"- {fallback}"


def _clip_text(text: str, max_chars: int = 280) -> str:
    """Trim text to a clean snippet without cutting off mid-word too aggressively."""
    cleaned = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(cleaned) <= max_chars:
        return cleaned

    clipped = cleaned[: max_chars - 1].rsplit(" ", 1)[0].strip()
    if not clipped:
        clipped = cleaned[: max_chars - 1].strip()
    return f"{clipped}…"


def _build_news_context_line(item: NewsItem, detail_chars: int = 260) -> str:
    """Format one news item with enough supporting detail for synthesis prompts."""
    teams = ", ".join(item.teams) if item.teams else "No tagged team"
    lines = [f"- [{teams}] {item.title}"]

    detail = item.summary or item.full_text
    detail = _clip_text(detail, detail_chars)
    if detail and detail.lower() != item.title.lower():
        lines.append(f"  Detail: {detail}")
    lines.append(f"  Source: {item.source}")
    return "\n".join(lines)


def _build_transcript_context_line(
    transcript: Transcript,
    detail_chars: int = 220,
) -> str:
    """Format one press transcript with a compact evidence line."""
    detail = transcript.ai_summary or _clip_text(transcript.text, detail_chars)
    detail = _clip_text(detail, detail_chars)

    lines = [f"- [{transcript.team}] {transcript.title}"]
    if detail:
        lines.append(f"  Detail: {detail}")
    lines.append(f"  Source: {transcript.channel_name}")
    return "\n".join(lines)


def summarize_transactions(
    items: list[NewsItem],
    client: Optional[Any] = None,
    usage_tracker: Optional[dict[str, Any]] = None,
) -> str:
    """Generate a transactions & signings summary."""
    if not items:
        return "No notable transactions reported today."

    client, runtime = _resolve_client_and_runtime(client)

    lines = []
    for item in items:
        teams = ", ".join(item.teams) if item.teams else "Unknown"
        lines.append(f"- [{teams}] {item.title}")
        if item.summary:
            lines.append(f"  {item.summary[:300]}")

    prompt = f"""Summarize today's NFL transactions and signings into a clear briefing.
List every transaction — do not omit or skip any, regardless of significance.
For each move, briefly note the impact.

Today's transactions:
{chr(10).join(lines)}"""

    return _call_model(
        client,
        prompt,
        runtime,
        usage_tracker=usage_tracker,
        usage_label="transactions",
    )


def summarize_injuries(
    items: list[NewsItem],
    client: Optional[Any] = None,
    usage_tracker: Optional[dict[str, Any]] = None,
) -> str:
    """Generate an injury report summary."""
    if not items:
        return "No injury report updates today."

    client, runtime = _resolve_client_and_runtime(client)

    lines = []
    for item in items:
        teams = ", ".join(item.teams) if item.teams else "Unknown"
        lines.append(f"## {teams} - {item.title}")
        if item.full_text:
            lines.append(item.full_text[:500])

    prompt = f"""Summarize today's NFL injury updates into a briefing.
Focus on key players whose status has changed or who are newly listed.
Note any players returning from injury.

Today's injury data:
{chr(10).join(lines)}"""

    return _call_model(
        client,
        prompt,
        runtime,
        usage_tracker=usage_tracker,
        usage_label="injuries",
    )


def summarize_press_conferences(
    transcripts: list[Transcript],
    client: Optional[Any] = None,
    usage_tracker: Optional[dict[str, Any]] = None,
) -> tuple[str, int]:
    """Generate press conference highlights from transcripts.

    Returns:
        Tuple of (summary_text, count_of_summarized_transcripts).
    """
    if not transcripts:
        return "No press conference transcripts available today.", 0

    client, runtime = _resolve_client_and_runtime(client)
    selected_transcripts = _select_press_transcripts(
        transcripts,
        limit=PRESS_MAX_ITEMS,
    )

    if not selected_transcripts:
        return "No high-signal press conference highlights today.", 0

    summaries = []
    for t in selected_transcripts:
        text = t.text[:6000] if len(t.text) > 6000 else t.text

        individual_prompt = f"""Read this NFL press conference transcript and return exactly two bullet points.

Bullet 1: the most concrete new information, quote paraphrase, or takeaway from the transcript.
Bullet 2: why it matters or what to watch next.

Rules:
- Use only information from the transcript.
- If the transcript contains little real news, say that clearly.
- Prefer direct paraphrases of what was actually said over speculation.
- Keep the full response under 70 words.
- Return bullet points only.

Team: {t.team} - {t.channel_name}
Title: {t.title}

Transcript:
{text}"""

        summary = _call_model(
            client,
            individual_prompt,
            runtime,
            max_tokens=220,
            usage_tracker=usage_tracker,
            usage_label=f"press:{t.team}",
        )
        summary = _normalize_bullet_summary(summary, max_bullets=2)
        t.ai_summary = summary
        summaries.append(f"**{t.team} - {t.title}**\n{summary}")

    return "\n\n".join(summaries), len(selected_transcripts)


def summarize_analysis(
    news_items: list[NewsItem],
    transcripts: list[Transcript],
    client: Optional[Any] = None,
    usage_tracker: Optional[dict[str, Any]] = None,
) -> str:
    """Generate an analysis / what-to-watch section."""
    client, runtime = _resolve_client_and_runtime(client)
    selected_transcripts = _select_press_transcripts(transcripts, limit=PRESS_MAX_ITEMS)
    ordered_news = sorted(
        news_items,
        key=lambda item: item.published,
        reverse=True,
    )

    headlines = []
    for item in ordered_news[:ANALYSIS_NEWS_LIMIT]:
        headlines.append(_build_news_context_line(item, detail_chars=280))

    transcript_titles = []
    for t in selected_transcripts:
        transcript_titles.append(_build_transcript_context_line(t, detail_chars=220))

    prompt = f"""Based only on today's NFL news and press conferences, write a detailed "Analysis & What to Watch" section for a knowledgeable NFL reader.

Output structure:
## Biggest Storylines
Use 2-4 substantial bullets or short paragraphs.

## Team / Offseason Signals
Synthesize recurring themes around coaching comments, roster-building direction, scheme clues, or organizational priorities when the evidence supports them.

## Player / Roster Notes
Call out the most meaningful player-specific or position-group developments.

## What to Watch Next
List 3-6 concrete follow-ups for the next few days.

Rules:
- Synthesize related items instead of listing headlines one by one.
- When several items point to one team's direction, connect them into a mini-dossier.
- Name specific players, coaches, executives, and teams whenever the source material supports it.
- Prefer depth and specificity over brevity.
- If a detail is missing, say it is not specified.
- Do not invent contract terms, timelines, scheme changes, or quotes.
- Do not end with a question, an offer to help, or any meta-commentary.

Today's news headlines:
{chr(10).join(headlines)}

Press conferences covered:
{chr(10).join(transcript_titles) if transcript_titles else "None today."}"""

    return _call_model(
        client,
        prompt,
        runtime,
        max_tokens=8192,
        usage_tracker=usage_tracker,
        usage_label="analysis",
        verbosity="medium",
        reasoning_effort="medium",
    )


def generate_team_highlights(
    news_items: list[NewsItem],
    transcripts: list[Transcript],
    client: Optional[Any] = None,
    usage_tracker: Optional[dict[str, Any]] = None,
) -> dict[str, str]:
    """Generate per-team highlight blurbs for teams with significant news."""
    client, runtime = _resolve_client_and_runtime(client)
    selected_transcripts = _select_press_transcripts(transcripts)

    team_items: dict[str, list] = {}
    for item in news_items:
        for team in item.teams:
            team_items.setdefault(team, []).append(item)
    for t in selected_transcripts:
        team_items.setdefault(t.team, []).append(t)

    highlights = {}
    for team, items in team_items.items():
        items = sorted(items, key=lambda item: item.published, reverse=True)

        lines = []
        for item in items[:TEAM_HIGHLIGHT_ITEM_LIMIT]:
            if isinstance(item, NewsItem):
                lines.append(_build_news_context_line(item, detail_chars=220))
            else:
                lines.append(_build_transcript_context_line(item, detail_chars=180))

        item_block = chr(10).join(lines)

        if len(items) < 2:
            # Single-source teams: let the LLM decide if it's noteworthy
            prompt = f"""Decide whether this item contains real, actionable NFL news for {team}, then either write a team note or skip.

Real news = roster moves, injury updates, contract talks, draft strategy signals, coaching decisions, front-office quotes with substance.
NOT real news = mock draft rankings, historical trivia, uniform reveals, podcast promos, general previews with no new information.

If noteworthy: write 1-2 sentences covering what happened and why it matters.
If NOT noteworthy: respond with exactly "SKIP" and nothing else.

Today's item:
{item_block}"""

            result = _call_model(
                client,
                prompt,
                runtime,
                max_tokens=200,
                usage_tracker=usage_tracker,
                usage_label=f"team:{team}",
                verbosity="low",
                reasoning_effort="low",
            )
            if result.strip().upper() != "SKIP":
                highlights[team] = result
            continue

        prompt = f"""Write a detailed team note for {team} based only on today's items.

Cover:
- the most important developments involving this team today
- any player-specific evaluations, roster implications, or coaching/front-office signals
- what this may mean next for the team

Rules:
- Prefer one strong paragraph or two short paragraphs.
- Connect related items into a coherent team outlook instead of listing headlines.
- Name the specific players, coaches, or executives involved.
- Prefer the team's full common name in the prose instead of the abbreviation when the source material makes it clear.
- If a detail is missing, say it is not specified.
- Keep the response under 140 words.

Today's items:
{item_block}"""

        highlights[team] = _call_model(
            client,
            prompt,
            runtime,
            max_tokens=550,
            usage_tracker=usage_tracker,
            usage_label=f"team:{team}",
            verbosity="medium",
            reasoning_effort="low",
        )

    return highlights


def run_summarization(
    news_items: list[NewsItem],
    transcripts: list[Transcript],
) -> dict:
    """Run the full summarization pipeline."""
    runtime = _get_runtime_config()
    client = _get_client(runtime["provider"])
    usage_tracker = _init_usage_tracker(runtime)

    transactions = [i for i in news_items if i.category == "transaction"]
    injuries = [i for i in news_items if i.category == "injury"]
    general_news = [
        i for i in news_items
        if i.category not in ("transaction", "injury")
    ]

    logger.info(
        "Summarizing with %s/%s: %d transactions, %d injuries, %d general news, %d transcripts",
        runtime["provider"],
        runtime["model"],
        len(transactions),
        len(injuries),
        len(general_news),
        len(transcripts),
    )

    sections = {}

    logger.info("Generating transactions summary...")
    sections["transactions"] = {
        "summary": summarize_transactions(
            transactions,
            client,
            usage_tracker=usage_tracker,
        ),
        "count": len(transactions),
    }

    logger.info("Generating injury report...")
    sections["injuries"] = {
        "summary": summarize_injuries(
            injuries,
            client,
            usage_tracker=usage_tracker,
        ),
        "count": len(injuries),
    }

    logger.info("Generating press conference highlights...")
    press_summary, press_count = summarize_press_conferences(
        transcripts,
        client,
        usage_tracker=usage_tracker,
    )
    sections["press_conferences"] = {
        "summary": press_summary,
        "count": press_count,
    }

    logger.info("Generating analysis...")
    sections["analysis"] = {
        "summary": summarize_analysis(
            news_items,
            transcripts,
            client,
            usage_tracker=usage_tracker,
        ),
    }

    logger.info("Generating team highlights...")
    team_highlights = generate_team_highlights(
        news_items,
        transcripts,
        client,
        usage_tracker=usage_tracker,
    )

    if usage_tracker["provider"] == "openai":
        logger.info(
            (
                "LLM usage: %d requests | input=%d tokens "
                "(cached=%d, uncached=%d) | output=%d tokens "
                "(reasoning=%d) | estimated cost=$%.4f"
            ),
            usage_tracker["request_count"],
            usage_tracker["input_tokens"],
            usage_tracker["cached_input_tokens"],
            usage_tracker["uncached_input_tokens"],
            usage_tracker["output_tokens"],
            usage_tracker["reasoning_tokens"],
            usage_tracker["estimated_cost_usd"],
        )
    elif usage_tracker["provider"] == "anthropic":
        logger.info(
            (
                "LLM usage: %d requests | input=%d tokens "
                "(cached_read=%d, cache_created=%d, uncached=%d) | "
                "output=%d tokens | estimated cost=$%.4f"
            ),
            usage_tracker["request_count"],
            usage_tracker["input_tokens"],
            usage_tracker["cached_input_tokens"],
            usage_tracker.get("cache_creation_tokens", 0),
            usage_tracker["uncached_input_tokens"],
            usage_tracker["output_tokens"],
            usage_tracker["estimated_cost_usd"],
        )

    return {
        "sections": sections,
        "team_highlights": team_highlights,
        "llm_usage": usage_tracker,
    }
