"""Tests for the per-call model override + cost attribution and the
config-driven Team Notes (player-news) section tuning.

These cover the plumbing added so a single section can run on a different
model / reasoning effort without mis-pricing the run. LLM output quality
is not unit-testable; see docs/MIGRATION/plan for manual verification.
"""

import processing.summarizer as sm


# ── Fakes ────────────────────────────────────────────────────────────────

class _FakeResp:
    """Minimal stand-in for an OpenAI Responses API result."""

    def __init__(self, text="- **Player (RB)** note [1]", in_tok=1000, out_tok=500,
                 cached=0, reasoning=0, tier="default"):
        self.output_text = text
        self.service_tier = tier
        self.status = "completed"
        self.incomplete_details = None
        self.usage = {
            "input_tokens": in_tok,
            "input_tokens_details": {"cached_tokens": cached},
            "output_tokens": out_tok,
            "output_tokens_details": {"reasoning_tokens": reasoning},
            "total_tokens": in_tok + out_tok,
        }


class _FakeClient:
    """Captures the model kwarg passed to responses.create."""

    def __init__(self, resp=None):
        self.calls = []
        self._resp = resp or _FakeResp()

        client = self

        class _Responses:
            def create(self, **kwargs):
                client.calls.append(kwargs)
                return client._resp

        self.responses = _Responses()


def _runtime(model="gpt-5.4-mini", **extra):
    rt = {
        "provider": "openai",
        "model": model,
        "max_output_tokens": 1400,
        "reasoning_effort": "low",
        "service_tier": "default",
        "sections": {},
    }
    rt.update(extra)
    return rt


# ── Per-call model override + cost attribution ─────────────────────────────

def test_per_call_model_reaches_create():
    client = _FakeClient()
    sm._call_openai(client, "prompt", _runtime(), max_tokens=500, model="gpt-5.4")
    assert client.calls[0]["model"] == "gpt-5.4"


def test_no_override_uses_runtime_model():
    client = _FakeClient()
    sm._call_openai(client, "prompt", _runtime(model="gpt-5.4-mini"), max_tokens=500)
    assert client.calls[0]["model"] == "gpt-5.4-mini"


def test_cost_attribution_uses_per_call_model():
    tracker = sm._init_usage_tracker(_runtime())  # tracker model = mini
    resp = _FakeResp(in_tok=1000, out_tok=500)
    # Price this call as full gpt-5.4 even though runtime model is mini.
    sm._record_openai_usage(resp, _runtime(model="gpt-5.4-mini"), tracker,
                            usage_label="x", model="gpt-5.4")
    # gpt-5.4 default: input 2.50/M, output 15.00/M → 0.0025 + 0.0075
    assert abs(tracker["estimated_cost_usd"] - 0.01) < 1e-6
    assert tracker["pricing_model"] == "gpt-5.4"


def test_mixed_model_label_and_summed_cost():
    tracker = sm._init_usage_tracker(_runtime())
    rt = _runtime(model="gpt-5.4-mini")
    sm._record_openai_usage(_FakeResp(in_tok=1000, out_tok=500), rt, tracker,
                            usage_label="mini", model="gpt-5.4-mini")
    sm._record_openai_usage(_FakeResp(in_tok=1000, out_tok=500), rt, tracker,
                            usage_label="full", model="gpt-5.4")
    # mini: 0.00075 + 0.00225 = 0.003 ; full: 0.01 ; total 0.013
    assert abs(tracker["estimated_cost_usd"] - 0.013) < 1e-6
    assert tracker["pricing_model"] == "mixed"
    assert len(tracker["operations"]) == 2


def test_same_model_twice_is_not_mixed():
    tracker = sm._init_usage_tracker(_runtime())
    rt = _runtime(model="gpt-5.4-mini")
    for _ in range(2):
        sm._record_openai_usage(_FakeResp(), rt, tracker, model="gpt-5.4-mini")
    assert tracker["pricing_model"] == "gpt-5.4-mini"


# ── team_news section resolution in _build_team_highlights_for_pool ────────

def _two_item_pool(make_item):
    return {
        "KC": [
            make_item("Chiefs RB rotation update", source="ESPN NFL", teams=["KC"]),
            make_item("Chiefs camp notes: backfield", source="Pro Football Talk", teams=["KC"]),
        ]
    }


def test_team_news_overrides_reach_call(monkeypatch, make_item):
    captured = []
    monkeypatch.setattr(sm, "_call_model",
                        lambda *a, **k: captured.append(k) or "- **Back (RB)** x [1]")
    rt = _runtime(sections={"team_news": {
        "model": "gpt-5.4", "reasoning_effort": "high", "max_output_tokens": 1600,
    }})
    sm._build_team_highlights_for_pool(_two_item_pool(make_item), client=None,
                                       runtime=rt, usage_tracker=None,
                                       is_transcript_pool=False)
    assert captured, "expected a team-notes LLM call"
    k = captured[-1]
    assert k["model"] == "gpt-5.4"
    assert k["reasoning_effort"] == "high"
    assert k["max_tokens"] == 1600


def test_team_news_defaults_when_absent(monkeypatch, make_item):
    captured = []
    monkeypatch.setattr(sm, "_call_model",
                        lambda *a, **k: captured.append(k) or "- **Back (RB)** x [1]")
    sm._build_team_highlights_for_pool(_two_item_pool(make_item), client=None,
                                       runtime=_runtime(), usage_tracker=None,
                                       is_transcript_pool=False)
    k = captured[-1]
    assert k["model"] is None          # falls back to runtime model downstream
    assert k["reasoning_effort"] == "low"
    assert k["max_tokens"] == 1400


class _SequencedClient:
    """Returns queued responses in order; records effort + model per call."""

    def __init__(self, responses):
        self._queue = list(responses)
        self.efforts = []
        self.models = []
        client = self

        class _Responses:
            def create(self, **kwargs):
                client.efforts.append(kwargs.get("reasoning", {}).get("effort"))
                client.models.append(kwargs.get("model"))
                return client._queue.pop(0)

        self.responses = _Responses()


def test_model_swap_fallback_after_two_no_text():
    """medium attempt + retry both reasoning-only → final attempt swaps model."""
    no_text = _FakeResp(text="", out_tok=500, reasoning=495)   # share .99
    good = _FakeResp(text="- **Player (WR)** real note [1]")
    client = _SequencedClient([no_text, no_text, good])
    out = sm._call_openai(client, "p", _runtime(model="gpt-5.4-mini"), max_tokens=500,
                          reasoning_effort="medium", usage_label="team:MIN")
    assert out == "- **Player (WR)** real note [1]"
    # 3 calls: medium, medium (retry), then low-effort on the SWAPPED model.
    assert client.efforts == ["medium", "medium", "low"]
    assert client.models == ["gpt-5.4-mini", "gpt-5.4-mini", "gpt-5.4"]


def test_model_swap_fires_even_when_already_low():
    """Same-input retries are futile, so the model swap fires regardless of effort."""
    no_text = _FakeResp(text="", out_tok=500, reasoning=495)
    good = _FakeResp(text="- **Player (WR)** note [1]")
    client = _SequencedClient([no_text, no_text, good])
    out = sm._call_openai(client, "p", _runtime(model="gpt-5.4"), max_tokens=500,
                          reasoning_effort="low", usage_label="x")
    assert out == "- **Player (WR)** note [1]"
    # started on full gpt-5.4 → fallback swaps DOWN to mini
    assert client.models == ["gpt-5.4", "gpt-5.4", "gpt-5.4-mini"]


def test_all_attempts_fail_returns_unavailable():
    """If even the model swap yields no text, fall back to the unavailable message."""
    no_text = _FakeResp(text="", out_tok=500, reasoning=495)
    client = _SequencedClient([no_text, no_text, no_text])
    out = sm._call_openai(client, "p", _runtime(model="gpt-5.4-mini"), max_tokens=500,
                          reasoning_effort="medium", usage_label="x")
    assert "Summary unavailable" in out
    assert client.models == ["gpt-5.4-mini", "gpt-5.4-mini", "gpt-5.4"]


def test_retry_when_text_present_is_false():
    assert sm._should_retry_openai_no_text(_FakeResp(text="real text"), "real text") is False


def test_retry_when_reasoning_dominates_completed_response():
    """The GB/MIA case: 'completed', no visible text, ~99% reasoning."""
    resp = _FakeResp(text="", out_tok=838, reasoning=832, tier="default")
    assert resp.status == "completed"
    assert sm._should_retry_openai_no_text(resp, "") is True


def test_retry_when_output_equals_reasoning():
    """Old exact-equality case still triggers (regression guard)."""
    resp = _FakeResp(text="", out_tok=500, reasoning=500)
    assert sm._should_retry_openai_no_text(resp, "") is True


def test_no_retry_for_genuine_empty_low_reasoning():
    """No text but reasoning is a small share → don't loop; it's a real empty."""
    resp = _FakeResp(text="", out_tok=1000, reasoning=100)
    assert sm._should_retry_openai_no_text(resp, "") is False


def test_retry_when_status_incomplete():
    resp = _FakeResp(text="", out_tok=100, reasoning=0)
    resp.status = "incomplete"
    assert sm._should_retry_openai_no_text(resp, "") is True


def test_transcript_pool_ignores_team_news(monkeypatch, make_item):
    """A transcript pool must keep its own tuning, not the team_news block."""
    from datetime import datetime, timezone
    from models import Transcript

    captured = []
    monkeypatch.setattr(sm, "_call_model",
                        lambda *a, **k: captured.append(k) or "- **Coach** x [1]")
    pool = {"KC": [
        Transcript("v1", "KC presser 1", "KC", "Chiefs", datetime(2026, 5, 1, tzinfo=timezone.utc),
                   "https://y/1", "text one", "captions"),
        Transcript("v2", "KC presser 2", "KC", "Chiefs", datetime(2026, 5, 1, tzinfo=timezone.utc),
                   "https://y/2", "text two", "captions"),
    ]}
    rt = _runtime(sections={"team_news": {"model": "gpt-5.4", "reasoning_effort": "high"}})
    sm._build_team_highlights_for_pool(pool, client=None, runtime=rt,
                                       usage_tracker=None, is_transcript_pool=True)
    k = captured[-1]
    # transcript branch hardcodes medium and passes no model override
    assert k.get("model") is None
    assert k["reasoning_effort"] == "medium"
