"""Trigger .github/workflows/refresh.yml from the dashboard, and report on it.

Rosters, transactions, practice-squad elevations, injury designations and
game-day inactives all move faster than this repo's crons — and GitHub fires
those crons a median 242 minutes late on top of that. Locally there is
``scripts/refresh_now.bat`` (Ctrl+Alt+N); on Streamlit Cloud there was nothing
but a note telling visitors to go find the repo's Actions tab. This module is
that button.

**Why not ``_repo_sync``.** That module is scoped to pushing small
visitor-edited JSON files through the Contents API. This is a different API
(Actions), a different PAT permission, a different failure taxonomy, and it
needs run-tracking state. It imports the repo/branch config from there so those
constants live in exactly one place.

**The PAT needs one more permission than ``_repo_sync`` does.** The
fine-grained token in ``st.secrets["GITHUB_PAT"]`` currently only needs
Contents: Read and write; dispatching also needs **Actions: Read and write**.
Without it every dispatch returns 403 — which `dispatch_refresh` translates
into exactly that sentence rather than a bare status code.

**Correlating the run.** ``POST /dispatches`` answers 204 with no body and no
run id, so a naive "newest run" lookup races every other dispatch. Instead we
mint a nonce, pass it as a workflow input, and ``refresh.yml`` puts it in its
``run-name:`` — which the API returns as ``display_title``. Matching is then
exact, with a timestamp fallback for runs GitHub hasn't titled yet.

**Local machines dispatch too, they don't run the work.** The cloud job is the
one that counts: it commits ``data/`` back to the repo, which is what the
dashboard and the report read. A local ``run_afternoon.py --only`` would update
one laptop and leave the site stale. So with no PAT but a working ``gh`` CLI we
shell out exactly like ``scripts/refresh_now.bat`` does.
"""

from __future__ import annotations

import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Iterable, Optional, Sequence

from dashboard._repo_sync import BRANCH, REPO_NAME, REPO_OWNER, _GITHUB_API, _get_pat
from dashboard.helpers import running_locally

WORKFLOW_FILE = "refresh.yml"

# A refresh is 2-4 minutes of CI, not a 2-second file PUT, so this is longer
# than _repo_sync's 60s autopush throttle. It is deliberately process-global:
# the Streamlit Cloud app is one process serving every visitor, and the point is
# to throttle the workflow, not one browser tab.
DISPATCH_COOLDOWN_SECONDS = 180

# What each button offers. Keys must match scripts.run_afternoon.REFRESH_TARGETS
# (tests/test_workflow_dispatch.py pins that).
TARGETS: dict[str, dict] = {
    "roster": {
        "label": "Rosters",
        "blurb": "nflverse active/PS/IR status for all 32 clubs, plus roster events and state.",
    },
    "elevations": {
        "label": "Elevations",
        "blurb": "Practice-squad elevations from ESPN's transaction feed.",
    },
    "injuries": {
        "label": "Injury report",
        "blurb": "Practice reports and game designations from the club sites, RotoWire and NFL.com.",
    },
    "inactives": {
        "label": "Inactives",
        "blurb": "Game-day inactives from ESPN's per-game rosters (a no-op outside a game window).",
    },
    "transactions": {
        "label": "Transactions",
        "blurb": "NFL.com signings, releases, reserve-list and waiver moves.",
    },
}


# ---------------------------------------------------------------------------
# Process-global dispatch bookkeeping
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_last_dispatch_at: float = 0.0
_last_dispatch: Optional[dict] = None       # {"nonce", "targets", "at", "html_url"}


def _reset_for_tests() -> None:
    """Clear the module-global throttle state (used by tests)."""
    global _last_dispatch_at, _last_dispatch
    with _lock:
        _last_dispatch_at = 0.0
        _last_dispatch = None


def last_dispatch() -> Optional[dict]:
    return dict(_last_dispatch) if _last_dispatch else None


def cooldown_remaining() -> float:
    """Seconds until another dispatch is allowed; 0.0 when clear."""
    if not _last_dispatch_at:
        return 0.0
    return max(0.0, DISPATCH_COOLDOWN_SECONDS - (time.time() - _last_dispatch_at))


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------


def _requests():
    """Seam so tests can swap the HTTP client without patching sys.modules."""
    import requests
    return requests


def _gh_available() -> bool:
    import shutil
    return bool(shutil.which("gh"))


def _transport() -> str:
    """``"api"`` (PAT), ``"gh"`` (local CLI) or ``"none"``."""
    if _get_pat():
        return "api"
    if running_locally() and _gh_available():
        return "gh"
    return "none"


def has_dispatch_pat() -> bool:
    return bool(_get_pat())


def can_dispatch() -> bool:
    return _transport() != "none"


_NO_TRANSPORT_MSG = (
    "Streamlit secret `GITHUB_PAT` is not configured. Add a fine-grained PAT "
    "for this repo with **Actions: Read and write** (plus Contents: Read and "
    "write, which the Save-to-repo buttons already use)."
)


def _headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _workflow_url(suffix: str = "") -> str:
    base = f"{_GITHUB_API}/repos/{REPO_OWNER}/{REPO_NAME}/actions/workflows/{WORKFLOW_FILE}"
    return base + suffix


def actions_html_url() -> str:
    return f"https://github.com/{REPO_OWNER}/{REPO_NAME}/actions/workflows/{WORKFLOW_FILE}"


def _normalize(targets: Sequence[str] | Iterable[str] | str) -> str:
    if isinstance(targets, str):
        items = [t.strip() for t in targets.split(",")]
    else:
        items = [str(t).strip() for t in targets]
    items = [t for t in items if t]
    return ",".join(items) if items else "all"


def _dispatch_error(status: int, body: str, headers: dict) -> str:
    """Translate a GitHub status into something that names the actual fix."""
    if status == 401:
        return "PAT rejected (401). The token is invalid or has expired."
    if status == 403:
        if str(headers.get("x-ratelimit-remaining", "")) == "0":
            reset = headers.get("x-ratelimit-reset")
            when = ""
            if reset:
                try:
                    when = " Resets at " + datetime.fromtimestamp(
                        int(reset), tz=timezone.utc).astimezone().strftime("%H:%M") + "."
                except (TypeError, ValueError, OSError):
                    when = ""
            return f"GitHub API rate limit reached (403).{when}"
        if "disabled" in body.lower():
            return ("The refresh workflow is disabled in the repo's Actions tab (403). "
                    "Re-enable \"On-demand refresh\" there.")
        return ("PAT lacks the Actions permission (403). A fine-grained token needs "
                "**Actions: Read and write** — Contents alone is not enough.")
    if status == 404:
        return (f"Workflow not found (404). Either `{WORKFLOW_FILE}` isn't on {BRANCH} yet, or the "
                "PAT has no access to this repo — fine-grained tokens answer 404, not 403, when "
                "the repo is outside their scope.")
    if status == 422:
        return (f"GitHub rejected the inputs (422): {body[:200]}. The dashboard and "
                f".github/workflows/{WORKFLOW_FILE} have drifted apart.")
    return f"GitHub API returned {status}: {body[:200]}"


def dispatch_refresh(targets: Sequence[str] | str, *, date: Optional[str] = None,
                     requested_by: str = "dashboard") -> tuple[bool, str, str]:
    """Start a refresh run. Returns ``(ok, human_message, nonce)``.

    The nonce is how the caller finds the resulting run later; it is empty on
    failure.
    """
    global _last_dispatch_at, _last_dispatch

    remaining = cooldown_remaining()
    if remaining > 0:
        ago = int(time.time() - _last_dispatch_at)
        return False, (f"A refresh was started {ago}s ago — available again in "
                       f"{int(remaining)}s."), ""

    transport = _transport()
    if transport == "none":
        return False, _NO_TRANSPORT_MSG, ""

    spec = _normalize(targets)
    nonce = uuid.uuid4().hex[:8]
    inputs = {
        "targets": spec,
        "date": date or "",
        "nonce": nonce,
        # Which transport actually sent it, so a run in the Actions list says
        # whether it came from the deployed site (PAT) or someone's local
        # dashboard (gh CLI). Without this the two are indistinguishable, which
        # makes "is the PAT working?" unanswerable from the run record.
        "requested_by": f"{requested_by}-local" if transport == "gh" else requested_by,
    }

    if transport == "gh":
        ok, msg = _dispatch_via_gh(inputs)
    else:
        ok, msg = _dispatch_via_api(inputs)

    if ok:
        with _lock:
            _last_dispatch_at = time.time()
            _last_dispatch = {"nonce": nonce, "targets": spec, "at": _last_dispatch_at,
                              "html_url": actions_html_url()}
        return True, msg, nonce
    return False, msg, ""


def _dispatch_via_api(inputs: dict) -> tuple[bool, str]:
    token = _get_pat()
    if not token:
        return False, _NO_TRANSPORT_MSG
    requests = _requests()
    try:
        r = requests.post(_workflow_url("/dispatches"), headers=_headers(token),
                          json={"ref": BRANCH, "inputs": inputs}, timeout=20)
    except Exception as e:  # noqa: BLE001
        return False, f"Network error dispatching the refresh: {e}"
    if r.status_code in (200, 201, 204):
        return True, (f"Refresh dispatched ({inputs['targets']}). GitHub Actions usually finishes "
                      "in 2-4 minutes; the timestamps above move once it commits.")
    return False, _dispatch_error(r.status_code, getattr(r, "text", "") or "",
                                  dict(getattr(r, "headers", {}) or {}))


def _dispatch_via_gh(inputs: dict) -> tuple[bool, str]:
    """Local fallback: the same `gh workflow run` the .bat dispatchers use."""
    cmd = ["gh", "workflow", "run", WORKFLOW_FILE, "--ref", BRANCH]
    for k, v in inputs.items():
        cmd += ["-f", f"{k}={v}"]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except Exception as e:  # noqa: BLE001
        return False, f"Could not run the gh CLI: {e}"
    if p.returncode == 0:
        return True, (f"Refresh dispatched via the gh CLI ({inputs['targets']}). "
                      "Watch it in the repo's Actions tab.")
    err = (p.stderr or p.stdout or "").strip().splitlines()
    return False, f"gh workflow run failed: {err[0] if err else 'unknown error'}"


# ---------------------------------------------------------------------------
# Finding and describing the run
# ---------------------------------------------------------------------------


def _epoch(iso: Optional[str]) -> float:
    if not iso:
        return 0.0
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _iso_utc(epoch: float) -> str:
    return datetime.fromtimestamp(max(0.0, epoch), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def find_run(nonce: str, *, since_epoch: float) -> Optional[dict]:
    """The workflow run this dispatch started, or None if it isn't listed yet.

    Matches the nonce in the run's display title. Falls back to the newest run
    started at/after the dispatch (minus a minute of clock skew), which covers a
    queued run — whose ``run_started_at`` is null — and a hand dispatch from the
    Actions tab that carried no nonce.
    """
    token = _get_pat()
    if not token:
        return None
    requests = _requests()
    params = {
        "event": "workflow_dispatch",
        "branch": BRANCH,
        "per_page": 10,
        "created": f">={_iso_utc(since_epoch - 120)}",
    }
    try:
        r = requests.get(_workflow_url("/runs"), headers=_headers(token), params=params, timeout=15)
    except Exception:  # noqa: BLE001
        return None
    if r.status_code != 200:
        return None
    try:
        runs = (r.json() or {}).get("workflow_runs") or []
    except Exception:  # noqa: BLE001
        return None

    if nonce:
        for run in runs:
            title = str(run.get("display_title") or run.get("name") or "")
            if nonce in title:
                return run
    for run in runs:
        started = _epoch(run.get("run_started_at") or run.get("created_at"))
        if started >= since_epoch - 60:
            return run
    return None


def describe_run(run: Optional[dict]) -> tuple[str, str]:
    """``(state_key, one human sentence)`` for a run from :func:`find_run`."""
    if not run:
        return "pending", "Dispatched — GitHub hasn't registered the run yet."
    status = str(run.get("status") or "")
    conclusion = str(run.get("conclusion") or "")
    when = ""
    started = _epoch(run.get("run_started_at") or run.get("created_at"))
    if started:
        when = datetime.fromtimestamp(started, tz=timezone.utc).astimezone().strftime("%H:%M")

    if status == "queued":
        return "queued", "Queued behind another pipeline run — it starts when that one finishes."
    if status in ("in_progress", "waiting", "requested", "pending"):
        return "running", f"Running since {when} (usually 2-4 minutes)."
    if status == "completed":
        if conclusion == "success":
            return "done", (f"Finished at {when}. If the timestamp above hasn't moved within a "
                            "minute, there was nothing new to collect.")
        if conclusion == "cancelled":
            # The daily-pipeline group keeps one pending run; a newer dispatch
            # replaces it. That is a supersede, not a failure.
            return "superseded", "Superseded by a newer refresh."
        return "failed", f"Run {conclusion or 'failed'} at {when} — open it for the log."
    return "pending", "Dispatched — GitHub hasn't registered the run yet."
