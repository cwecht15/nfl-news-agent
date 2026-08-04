"""Tests for the ESPN content-API body fetch in collectors.rss_collector.

No network: the session is a stub returning canned JSON.
"""

import collectors.rss_collector as rc


# ── _strip_story_html ───────────────────────────────────────────────────

def test_strip_story_html_basic():
    raw = "<p>First paragraph.</p><p>Second <b>bold</b> paragraph.</p>"
    out = rc._strip_story_html(raw)
    assert "First paragraph." in out
    assert "Second" in out and "bold" in out
    assert "<" not in out


def test_strip_story_html_removes_scripts():
    raw = "<p>Real text.</p><script>var x = 1;</script><style>p{}</style>"
    out = rc._strip_story_html(raw)
    assert "Real text." in out
    assert "var x" not in out
    assert "p{}" not in out


def test_strip_story_html_unescapes_entities():
    assert "Bears & Packers" in rc._strip_story_html("<p>Bears &amp; Packers</p>")


def test_strip_story_html_empty():
    assert rc._strip_story_html("") == ""
    assert rc._strip_story_html(None) == ""


# ── _fetch_espn_story_body ──────────────────────────────────────────────

class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _StubSession:
    def __init__(self, payload):
        self._payload = payload
        self.last_url = None

    def get(self, url, timeout=None):
        self.last_url = url
        return _Resp(self._payload)


class _RaisingSession:
    def get(self, url, timeout=None):
        raise RuntimeError("boom")


def test_fetch_espn_story_body_returns_stripped_story():
    session = _StubSession({"headlines": [{"story": "<p>Camp report body.</p>"}]})
    body = rc._fetch_espn_story_body(session, 12345, timeout=5)
    assert body == "Camp report body."
    assert "12345" in session.last_url


def test_fetch_espn_story_body_caps_length():
    long_story = "<p>" + ("word " * 5000) + "</p>"
    session = _StubSession({"headlines": [{"story": long_story}]})
    body = rc._fetch_espn_story_body(session, 1, timeout=5, max_chars=100)
    assert len(body) <= 101  # cap + ellipsis


def test_fetch_espn_story_body_missing_story():
    session = _StubSession({"headlines": [{}]})
    assert rc._fetch_espn_story_body(session, 1, timeout=5) == ""


def test_fetch_espn_story_body_empty_headlines():
    session = _StubSession({"headlines": []})
    assert rc._fetch_espn_story_body(session, 1, timeout=5) == ""


def test_fetch_espn_story_body_request_error():
    assert rc._fetch_espn_story_body(_RaisingSession(), 1, timeout=5) == ""


def test_fetch_espn_story_body_no_id():
    assert rc._fetch_espn_story_body(_StubSession({}), None, timeout=5) == ""
