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
LEAGUE_WIDE_NEWS_LIMIT = 25
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
            incomplete_details = _obj_get(response, "incomplete_details")
            incomplete_reason = _obj_get(incomplete_details, "reason")
            if incomplete_reason:
                logger.warning(
                    "OpenAI truncated %s output at max_output_tokens=%d (reason=%s); "
                    "consider raising the cap for this call",
                    usage_label or "summary",
                    max_tokens,
                    incomplete_reason,
                )
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

    # Prefer full_text — when present it carries article body content
    # (player-level detail for "depth chart" / "every pick" style pieces
    # that the title alone wouldn't hint at). Fall back to summary.
    detail = item.full_text or item.summary
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


# Side-tagged position abbreviations occasionally leak through as
# generic_pos in the OurLads scrape. Normalize the common ones so
# transaction bullets read like "DT Jay Tufele" not "LDT Jay Tufele".
_POSITION_NORMALIZATION = {
    "LDE": "DE", "RDE": "DE",
    "LDT": "DT", "RDT": "DT", "NT": "DT",
    "LCB": "CB", "RCB": "CB",
    "WLB": "LB", "MLB": "LB", "SLB": "LB",
    "FS": "S", "SS": "S",
    "LWR": "WR", "RWR": "WR", "SWR": "WR",
    "LT": "OL", "RT": "OL", "LG": "OL", "RG": "OL", "C": "OL",
}


def _normalize_position(pos: str) -> str:
    return _POSITION_NORMALIZATION.get(pos, pos)


def _load_position_lookup() -> dict[str, str]:
    """Build a lower(name) -> generic position map from the latest depth chart.

    Used to enrich transaction bullets with position. Falls back to {} if
    the depth chart collector or its data isn't available.
    """
    try:
        from collectors.depth_chart_collector import load_latest_depth_charts
        dc = load_latest_depth_charts() or {}
    except Exception:
        return {}

    lookup: dict[str, str] = {}
    for entry in dc.values():
        name = (entry.get("name") or "").strip().lower()
        pos = entry.get("generic_pos") or entry.get("pos") or ""
        if name and pos:
            lookup.setdefault(name, _normalize_position(pos))
    return lookup


def _position_for_title(title: str, position_lookup: dict[str, str]) -> str:
    """Best-effort position lookup from a transaction title.

    Title format is typically 'First Last: Team (Move type)'. We extract
    the segment before the first colon as the player name.
    """
    if not position_lookup or ":" not in title:
        return ""
    candidate = title.split(":", 1)[0].strip()
    candidate = re.sub(r"^\[.*?\]\s*", "", candidate)
    return position_lookup.get(candidate.lower(), "")


def summarize_transactions(
    items: list[NewsItem],
    client: Optional[Any] = None,
    usage_tracker: Optional[dict[str, Any]] = None,
    position_lookup: Optional[dict[str, str]] = None,
) -> str:
    """Generate a transactions & signings summary."""
    if not items:
        return "No notable transactions reported today."

    client, runtime = _resolve_client_and_runtime(client)
    position_lookup = position_lookup or {}

    lines = []
    for item in items:
        teams = ", ".join(item.teams) if item.teams else "Unknown"
        pos = _position_for_title(item.title, position_lookup)
        tag = f"[{teams} / {pos}]" if pos else f"[{teams}]"
        lines.append(f"- {tag} {item.title}")
        if item.summary:
            lines.append(f"  {item.summary[:300]}")

    prompt = f"""Summarize today's NFL transactions and signings into a clear briefing.
List every transaction — do not omit or skip any, regardless of significance.
For each move, briefly note the impact.

Each input line is tagged like `[TEAM / POS]` or `[TEAM]`. The position tag
comes from the team's depth chart and is reliable when present. If a line
has a position tag, include the position in your bullet (e.g. "Lions signed
LB Joe Bachie"). If a line has no position tag, do not invent one — just
omit the position.

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
        text = t.text[:10000] if len(t.text) > 10000 else t.text

        individual_prompt = f"""Read this NFL press conference transcript and write a substantive briefing of the actual content.

Output 4–6 bullets covering distinct, concrete points. Cover whichever of these are present in the transcript: roster decisions, player evaluations and where guys fit, scheme/coaching philosophy, injury or rehab updates, position competitions, depth-chart implications, contract or front-office signals, schedule or practice plans, and direct paraphrased quotes that carry real substance. Lead each bullet with the most specific named subject in bold (player, coach, exec, position group). End each bullet with a brief "what to watch next" only when it adds something the transcript actually supports.

Rules:
- Use only information from the transcript. Do not extrapolate.
- Prefer concrete paraphrases (numbers, role descriptions, named matchups) over generalities.
- If a bullet is just a generic platitude ("we're excited about the kid", "we're going to compete"), drop it.
- If the transcript genuinely contains nothing of substance, say "Limited substantive news — mostly platitudes." and stop. Do not pad.
- NEVER invent a player's first name, jersey number, position, or stat. If only a last name is given, use only the last name.
- Keep the full response under 220 words.
- Return bullet points only — no headers, no preamble.

Team: {t.team} - {t.channel_name}
Title: {t.title}

Transcript:
{text}"""

        summary = _call_model(
            client,
            individual_prompt,
            runtime,
            max_tokens=2500,
            usage_tracker=usage_tracker,
            usage_label=f"press:{t.team}",
            reasoning_effort="medium",
        )
        summary = _normalize_bullet_summary(summary, max_bullets=6)
        t.ai_summary = summary
        summaries.append(f"**{t.team} - {t.title}**\n{summary}")

    return "\n\n".join(summaries), len(selected_transcripts)


def summarize_league_wide(
    news_items: list[NewsItem],
    client: Optional[Any] = None,
    usage_tracker: Optional[dict[str, Any]] = None,
) -> tuple[str, list[dict]]:
    """Summarize cross-team / league-office news items into a flat bullet list.

    Input is restricted to items with no specific team affiliation
    (e.g., CBA progress, league-wide mock drafts, scouting roundups).
    Items already covered by the Transactions / Injuries sections must
    be excluded by the caller.

    Returns (summary_text, numbered_sources). Sources never cited inline
    are dropped from the returned list.
    """
    client, runtime = _resolve_client_and_runtime(client)

    # Pick the most recent league-wide items.
    ordered = sorted(news_items, key=lambda item: item.published, reverse=True)
    selected = ordered[:LEAGUE_WIDE_NEWS_LIMIT]

    if not selected:
        return "No league-wide notes today.", []

    headlines = []
    candidate_sources: list[dict] = []
    for idx, item in enumerate(selected, start=1):
        line = _build_news_context_line(item, detail_chars=260)
        headlines.append(f"[{idx}] {line}")
        candidate_sources.append({
            "num": idx,
            "title": item.title,
            "source": item.source,
            "url": item.url,
        })

    prompt = f"""Summarize today's league-wide / cross-team NFL items as a flat bullet list.

Scope: only items below — CBA news, league-office moves, mock drafts, multi-team scouting roundups, broadcast/business news. Per-team news, transactions, and injuries are handled in their own sections, so do NOT re-summarize team-specific stories here.

Output:
- One bullet per distinct item.
- Lead with the named subject (person, league office, or topic) in bold.
- Then 1 sentence on what happened and why it matters.
- End the bullet with the citation, e.g. [3] or [1, 4].
- Cap at 8 bullets total. Drop the lowest-signal items if you have more than 8.
- If nothing in the list is genuinely league-wide, output exactly: "No league-wide notes today."

Rules:
- Use only the items provided. If a detail is missing, say it is not specified.
- Do not invent timelines, contract terms, or quotes.
- No headers, no preamble, no closing meta-commentary.

Today's items:
{chr(10).join(headlines)}"""

    text = _call_model(
        client,
        prompt,
        runtime,
        max_tokens=4000,
        usage_tracker=usage_tracker,
        usage_label="league_wide",
        verbosity="medium",
        reasoning_effort="low",
    )

    cited_nums = set()
    for match in re.findall(r"\[(\d+(?:[,\s]+\d+)*)\]", text):
        for n in re.split(r"[,\s]+", match):
            if n.strip().isdigit():
                cited_nums.add(int(n))

    numbered_sources = [s for s in candidate_sources if s["num"] in cited_nums]
    return text, numbered_sources


def _trim_cited_sources(
    text: str,
    candidates: list[dict],
) -> list[dict]:
    """Drop sources that aren't referenced inline as [N] in the text."""
    cited: set[int] = set()
    for match in re.findall(r"\[(\d+(?:[,\s]+\d+)*)\]", text):
        for n in re.split(r"[,\s]+", match):
            if n.strip().isdigit():
                cited.add(int(n))
    return [s for s in candidates if s["num"] in cited]


# Title patterns that signal a "deep" team article worth force-including
# in the team-notes pool — these usually contain per-player breakdowns
# that the LLM can mine for specific notes.
_DEEP_ARTICLE_PATTERNS = re.compile(
    r"\b(depth chart|every pick|every selection|every player|"
    r"post[- ]draft|draft class|draft haul|draft grades?|"
    r"draft recap|two[- ]deep|projected starters?|day 1 starters?|"
    r"position by position|player by player|breakdown of|"
    r"who has the best chance|udfas? (?:to )?watch|"
    r"undrafted free agents?)\b",
    re.IGNORECASE,
)


def _is_deep_article(item) -> bool:
    """Heuristic: does this item probably contain per-player notes worth surfacing?"""
    if not isinstance(item, NewsItem):
        return False
    title = item.title or ""
    body = item.full_text or ""
    if _DEEP_ARTICLE_PATTERNS.search(title):
        return True
    # Long body alone isn't enough — many articles are length-padded with
    # quotes and recap. Pair length with the title heuristic instead.
    return False


def _diversify_by_source(
    items: list,
    limit: int,
    max_per_source: int | None = None,
    deep_reserve: int | None = None,
) -> list:
    """Pick up to `limit` items, prioritizing relevance with a soft anti-domination cap.

    Approach:
      1) Score each item: deep articles (depth charts, post-draft, UDFA
         trackers) outrank ordinary items; within each tier, prefer richer
         body content, then more recent items.
      2) Walk items in score order, soft-capping any single source at
         half of `limit` (so one outlet can carry up to half of a team's
         pool when warranted, but never all of it).
      3) Fill any remaining slots by score, ignoring the cap.
    """
    def _source_label(item) -> str:
        if isinstance(item, NewsItem):
            return item.source or "?"
        return getattr(item, "channel_name", "?") or "?"

    _PRIMARY_TITLE = re.compile(
        r"\b(depth chart|every pick|every selection|two[- ]deep|projected starters?|day 1 starters?)\b",
        re.IGNORECASE,
    )

    def _score(it) -> tuple:
        if not isinstance(it, NewsItem):
            # Transcripts: rank below news but ahead of nothing.
            return (0, 0, 0, getattr(it, "published", 0))
        title = it.title or ""
        body = it.full_text or ""
        primary = 1 if _PRIMARY_TITLE.search(title) else 0
        deep = 1 if _is_deep_article(it) else 0
        return (primary, deep, len(body), it.published)

    soft_cap = max(1, limit // 2)  # one source can't take more than half the slots
    ordered = sorted(items, key=_score, reverse=True)

    picked: list = []
    per_source: dict[str, int] = {}

    # Pass 1: respect the soft cap so no single outlet dominates.
    for it in ordered:
        if len(picked) >= limit:
            break
        src = _source_label(it)
        if per_source.get(src, 0) >= soft_cap:
            continue
        per_source[src] = per_source.get(src, 0) + 1
        picked.append(it)

    # Pass 2: if we couldn't fill the limit because of caps, fill from
    # whatever's left in score order (cap-busting is fine here — it means
    # the team only had two real sources in the pool).
    if len(picked) < limit:
        remaining = [it for it in ordered if it not in picked]
        picked.extend(remaining[: limit - len(picked)])

    return picked


def _build_team_item_lines(items: list) -> tuple[list[str], list[dict]]:
    """Format per-team items with [N] indexing and a parallel source list."""
    selected = _diversify_by_source(items, TEAM_HIGHLIGHT_ITEM_LIMIT)

    lines: list[str] = []
    candidates: list[dict] = []
    for idx, item in enumerate(selected, start=1):
        if isinstance(item, NewsItem):
            # Deep articles (depth charts, post-draft recaps, every-pick
            # breakdowns) cover many positions; show the LLM the whole
            # body so per-position notes beyond QB can surface. Ordinary
            # items get a smaller window to keep the prompt focused.
            window = 5000 if _is_deep_article(item) else 1200
            lines.append(f"[{idx}] " + _build_news_context_line(item, detail_chars=window))
            candidates.append({
                "num": idx,
                "title": item.title,
                "source": item.source,
                "url": item.url,
            })
        else:
            lines.append(f"[{idx}] " + _build_transcript_context_line(item, detail_chars=300))
            candidates.append({
                "num": idx,
                "title": item.title,
                "source": item.channel_name,
                "url": item.url,
            })
    return lines, candidates


def _build_team_highlights_for_pool(
    team_items: dict[str, list],
    client: Any,
    runtime: dict[str, Any],
    usage_tracker: Optional[dict[str, Any]] = None,
    is_transcript_pool: bool = False,
) -> dict[str, dict]:
    """Shared per-team highlight builder.

    Used by both the news-only path (`generate_team_highlights`) and the
    transcripts-only path (`summarize_team_highlights_from_transcripts`)
    that drives the YouTube section. The pool can mix NewsItem and
    Transcript objects — `_build_team_item_lines` handles both shapes.

    `is_transcript_pool=True` switches to a deeper prompt geared toward
    long-form press-conference content (more bullets, longer cap, medium
    reasoning effort).
    """
    highlights: dict[str, dict] = {}
    for team, items in team_items.items():
        items = sorted(items, key=lambda item: item.published, reverse=True)
        lines, candidates = _build_team_item_lines(items)
        item_block = chr(10).join(lines)

        if len(items) < 2:
            if is_transcript_pool:
                prompt = f"""Decide whether this {team} press-conference transcript contains real, actionable content, then either write a team note or skip.

Real content = roster decisions, player evaluations with specifics, scheme/coaching detail, injury or rehab specifics, position competitions, contract or front-office signals, substantive paraphrased quotes.
NOT real content = generic platitudes, hype, hello/intro pleasantries with no info, mock-draft chatter unrelated to roster moves.

If noteworthy: write 3–5 bullets covering distinct points. Lead each bullet with the most specific named subject in bold and end with the citation [1]. Keep the full response under 200 words.
If NOT noteworthy: respond with exactly "SKIP" and nothing else.

Today's transcript:
{item_block}"""
                result = _call_model(
                    client,
                    prompt,
                    runtime,
                    max_tokens=2000,
                    usage_tracker=usage_tracker,
                    usage_label=f"team:{team}",
                    verbosity="medium",
                    reasoning_effort="medium",
                )
            else:
                prompt = f"""Decide whether this item contains real, actionable NFL news for {team}, then either write a team note or skip.

Real news = roster moves, injury updates, contract talks, draft strategy signals, coaching decisions, front-office quotes with substance — and the item must name a {team} player, coach, or executive and say something specific about them.
NOT real news = mock draft rankings, historical trivia, uniform reveals, podcast promos, general previews with no new information, paywalled excerpts that only describe what the article will cover, items where the only {team}-related "subject" is the journalist or outlet, items where you would have to write "the excerpt does not specify any {team} player / decision / detail."

If noteworthy: write 1-2 sentences covering what happened and why it matters, and end with the citation [1].
If NOT noteworthy: respond with exactly "SKIP" and nothing else. When in doubt, SKIP — a missing team note is far better than a bullet that admits it has no {team} content.

Today's item:
{item_block}"""
                result = _call_model(
                    client,
                    prompt,
                    runtime,
                    max_tokens=250,
                    usage_tracker=usage_tracker,
                    usage_label=f"team:{team}",
                    verbosity="low",
                    reasoning_effort="low",
                )
            if result.strip().upper() != "SKIP":
                highlights[team] = {
                    "summary": result,
                    "numbered_sources": _trim_cited_sources(result, candidates),
                }
            continue

        if is_transcript_pool:
            prompt = f"""Write a substantive bulleted team note for {team} based only on today's press-conference transcripts.

Each input item is numbered like [1], [2], etc. — you MUST cite the items you use.

Output: a markdown bullet list of 6–10 bullets covering the distinct developments across the transcripts. Aim for breadth and depth: roster decisions, position competitions, scheme philosophy, injury / rehab specifics, depth-chart implications, contract / front-office signals, named-quote paraphrases with real substance.

Format each bullet as:
- **Player or coach or exec name** — what was said or decided in 1–2 sentences, then a short follow-up on why it matters or what's next when the transcripts actually support it. End the bullet with the citation, e.g. [3] or [1, 4].

Position priority (for fantasy-football relevance):
1. Highest priority: offensive skill players — QB, RB, FB, WR, TE.
2. Second: offensive line and head coach / OC / DC philosophy.
3. Lowest: defense (non-coordinator), kickers, punters, long snappers, special-teams roles.
Skill-position content beats equally-newsworthy non-skill content.

Rules:
- One bullet per development. Do not synthesize multiple unrelated items into one bullet.
- Lead each bullet with the most-specific named subject (player, coach, exec, or position group). For genuinely team-level points, lead with the topic in bold.
- Every bullet must end with at least one [N] citation.
- Surface NON-OBVIOUS specifics: unexpected starters, position competitions, depth-chart shake-ups beyond entrenched stars, late-round picks fighting for roles, UDFA fits, scheme tweaks. Do NOT restate common knowledge.
- Skip pure platitudes ("we're excited", "we're going to compete"). If a transcript only contains platitudes, ignore it.
- NEVER invent a player's first name, jersey number, position, or stat. If only a last name is given, use only the last name.
- Use only the information in the transcripts. If a detail is missing, say it is not specified.
- Keep the entire response under 350 words.
- No section headers, no preamble, no closing commentary — just the bullets.

Today's transcripts:
{item_block}"""

            text = _call_model(
                client,
                prompt,
                runtime,
                max_tokens=2500,
                usage_tracker=usage_tracker,
                usage_label=f"team:{team}",
                verbosity="medium",
                reasoning_effort="medium",
            )
        else:
            prompt = f"""Write a bulleted team note for {team} based only on today's items.

Each input item is numbered like [1], [2], etc. — you MUST cite the items you use.

Output: a markdown bullet list, one bullet per distinct development.

Format each bullet as:
- **Player or coach or exec name** — what happened in 1–2 sentences, then 1–2 short follow-up sentences on why it matters, what's next, or supporting context (snap share, depth-chart implications, scheme fit) when the items genuinely support it. End the bullet with the citation, e.g. [3] or [1, 4].

Position priority (for fantasy-football relevance):
1. Highest priority: offensive skill players — QB, RB, FB, WR, TE.
2. Second: offensive line and head coach / OC / DC moves.
3. Lowest: defense (non-coordinator), kickers, punters, long snappers, special-teams roles.
Prefer skill-position content over equally-newsworthy non-skill content. A WR3 competition is more interesting than a CB3 competition, all else equal. Include one bullet per genuinely distinct development the items support — don't pad to hit a target, but don't drop a substantive development just to stay short either.

Rules:
- One bullet per development. Do not synthesize multiple unrelated items into one bullet.
- Lead each bullet with the most-specific named subject (player, coach, or executive). If the item is genuinely team-level (not a person), lead with the topic in bold instead.
- Every bullet must end with at least one [N] citation pointing to the input item(s) that source it.
- Surface NON-OBVIOUS specifics: unexpected starters, position competitions, depth-chart shake-ups beyond the entrenched stars, late-round picks fighting for roles, UDFA roster fits. Do NOT write bullets that re-state common knowledge ("the franchise QB is still the starter", "the all-pro is still the WR1"). If a depth-chart article only confirms the obvious at QB, look at the article's notes on RB / WR / TE / OL instead.
- NEVER invent a player's first name, jersey number, position, or any other identifier. If the source refers to a player by last name only (e.g. "Jennings"), write the bullet using ONLY the last name as the source provides it (e.g. "**Jennings (RB)**"). Adding a wrong first name is worse than omitting it. Same for coaches and executives.
- Skip items that are pure trivia, mock drafts not roster-relevant, jersey reveals, or filler.
- DROP any item whose {team}-relevant content boils down to "the excerpt does not specify any {team} player / decision / detail" or where the only named subject is the journalist or outlet rather than a {team} player, coach, or exec. Skip the item entirely — do NOT write a bullet about the absence of information.
- Use only the information in today's items. If a detail is missing for an otherwise-substantive bullet, say it is not specified.
- Keep the entire response under 280 words. This is a ceiling, not a target — only use the room when the items genuinely support more detail.
- No section headers, no preamble, no closing commentary — just the bullets.

Today's items:
{item_block}"""

            text = _call_model(
                client,
                prompt,
                runtime,
                max_tokens=1400,
                usage_tracker=usage_tracker,
                usage_label=f"team:{team}",
                verbosity="medium",
                reasoning_effort="low",
            )
        highlights[team] = {
            "summary": text,
            "numbered_sources": _trim_cited_sources(text, candidates),
        }

    return highlights


def generate_team_highlights(
    news_items: list[NewsItem],
    client: Optional[Any] = None,
    usage_tracker: Optional[dict[str, Any]] = None,
) -> dict[str, dict]:
    """Generate per-team highlight blurbs from news items only.

    Transcripts are summarized separately by the YouTube section
    (`processing.yt_section.build_yt_section`) so the daily report stays
    independent of YouTube data availability.
    """
    client, runtime = _resolve_client_and_runtime(client)

    team_items: dict[str, list] = {}
    for item in news_items:
        if item.category in ("transaction", "injury"):
            continue
        for team in item.teams:
            team_items.setdefault(team, []).append(item)

    return _build_team_highlights_for_pool(
        team_items, client, runtime, usage_tracker,
    )


def summarize_team_highlights_from_transcripts(
    transcripts: list[Transcript],
    client: Optional[Any] = None,
    usage_tracker: Optional[dict[str, Any]] = None,
) -> dict[str, dict]:
    """Per-team highlights drawn solely from press-conference transcripts.

    Powers the per-team subsection of the YouTube report. Same prompt
    rules as `generate_team_highlights` — the pool just contains
    Transcript objects instead of NewsItem.
    """
    client, runtime = _resolve_client_and_runtime(client)
    selected_transcripts = _select_press_transcripts(transcripts)

    team_items: dict[str, list] = {}
    for t in selected_transcripts:
        team_items.setdefault(t.team, []).append(t)

    return _build_team_highlights_for_pool(
        team_items, client, runtime, usage_tracker,
        is_transcript_pool=True,
    )


def run_summarization(
    news_items: list[NewsItem],
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
        "Summarizing with %s/%s: %d transactions, %d injuries, %d general news",
        runtime["provider"],
        runtime["model"],
        len(transactions),
        len(injuries),
        len(general_news),
    )

    sections = {}

    position_lookup = _load_position_lookup()
    if position_lookup:
        logger.info("Loaded position lookup for %d players from depth charts", len(position_lookup))

    logger.info("Generating transactions summary...")
    sections["transactions"] = {
        "summary": summarize_transactions(
            transactions,
            client,
            usage_tracker=usage_tracker,
            position_lookup=position_lookup,
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

    logger.info("Generating league-wide notes...")
    league_wide_news = [i for i in general_news if not i.teams]
    league_wide_text, league_wide_sources = summarize_league_wide(
        league_wide_news,
        client,
        usage_tracker=usage_tracker,
    )
    sections["league_wide"] = {
        "summary": league_wide_text,
        "numbered_sources": league_wide_sources,
        "count": len(league_wide_news),
    }

    logger.info("Generating team notes...")
    team_highlights = generate_team_highlights(
        general_news,
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
