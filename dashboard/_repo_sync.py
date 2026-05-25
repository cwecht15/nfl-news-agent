"""Push small visitor-edited state files to origin/master via GitHub Contents API.

Exists because Streamlit Cloud's container has a writable but ephemeral
filesystem. When a visitor flags a finding or dismisses a transaction
through the UI we write to a JSON file inside the container, but that
file is discarded on every redeploy (a redeploy fires on any master
push, including unrelated code commits). The daily 10:00 UTC cron
commits these files back, but in-flight changes between cron runs are
at risk until they're persisted.

This module provides a manual escape hatch: pages render a "Save to
repo" button that calls `push_file_to_repo()`, which uses the GitHub
Contents API + a fine-grained PAT (stored in Streamlit secrets as
`GITHUB_PAT`) to commit the current file contents to master with a
`[skip ci]` marker. After this push the changes are durable across
redeploys.

`push_flag_store_to_repo` and `push_overrides_to_repo` are thin
wrappers around `push_file_to_repo` for the two files that have UIs
hooked up so far.

The PAT scope only needs:
  - Repository: cwecht15/nfl-news-agent
  - Permissions: Contents: Read and write
Generate at: github.com → Settings → Developer settings →
  Personal access tokens → Fine-grained tokens.
"""

from __future__ import annotations

import base64
import threading
import time
from pathlib import Path

import streamlit as st

# Module-level config — adjust if the repo or branch ever changes.
REPO_OWNER = "cwecht15"
REPO_NAME = "nfl-news-agent"
BRANCH = "master"
FLAG_FILE_PATH = "data/flagged_findings.json"
OVERRIDES_FILE_PATH = "data/projections/transaction_overrides.json"

_SECRET_KEY = "GITHUB_PAT"
_GITHUB_API = "https://api.github.com"


def _get_pat() -> str | None:
    """Return the configured PAT or None when the secret isn't set."""
    try:
        return st.secrets.get(_SECRET_KEY)
    except (FileNotFoundError, st.errors.StreamlitSecretNotFoundError):
        return None
    except Exception:
        return None


def has_pat_configured() -> bool:
    return bool(_get_pat())


def push_file_to_repo(
    repo_path: str,
    commit_message: str,
    success_label: str = "file",
) -> tuple[bool, str]:
    """Commit `<repo_root>/<repo_path>` to origin/master via GitHub Contents API.

    `repo_path` is the path inside the repo (also used as the local path
    relative to the project root). `commit_message` is the message
    written to the GitHub commit. `success_label` is the noun used in
    the success message rendered to the user (e.g. "flags", "dismissals").

    Returns (ok, human_message).
    """
    token = _get_pat()
    if not token:
        return False, (
            f"Streamlit secret `{_SECRET_KEY}` is not configured. Add a "
            "fine-grained PAT with Contents:write on this repo and try again."
        )

    project_root = Path(__file__).parent.parent
    local_path = project_root / repo_path
    if not local_path.exists():
        return False, f"Local file missing: {repo_path}"

    try:
        content_bytes = local_path.read_bytes()
    except Exception as e:
        return False, f"Could not read local file: {e}"

    content_b64 = base64.b64encode(content_bytes).decode("ascii")

    # Lazy import — `requests` is already in requirements but keeping
    # the import inside the function means a missing dep wouldn't
    # break the page on import time.
    import requests

    url = f"{_GITHUB_API}/repos/{REPO_OWNER}/{REPO_NAME}/contents/{repo_path}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    # GET current SHA — required by the PUT for non-create updates.
    sha: str | None = None
    try:
        r = requests.get(url, headers=headers, params={"ref": BRANCH}, timeout=15)
    except Exception as e:
        return False, f"Network error fetching current file SHA: {e}"
    if r.status_code == 200:
        try:
            sha = r.json().get("sha")
        except Exception:
            sha = None
    elif r.status_code == 404:
        # File not yet on remote — first-time commit, no SHA needed.
        sha = None
    elif r.status_code == 401:
        return False, "PAT rejected (401). Verify the token is valid + has Contents:write."
    elif r.status_code == 403:
        return False, "PAT lacks permission (403). Contents:write on this repo is required."
    else:
        return False, (
            f"GitHub API returned {r.status_code} fetching SHA: "
            f"{r.text[:200]}"
        )

    payload = {
        "message": commit_message,
        "content": content_b64,
        "branch": BRANCH,
    }
    if sha:
        payload["sha"] = sha

    try:
        r = requests.put(url, headers=headers, json=payload, timeout=20)
    except Exception as e:
        return False, f"Network error during push: {e}"

    if r.status_code in (200, 201):
        try:
            new_sha = r.json().get("commit", {}).get("sha", "")[:7]
        except Exception:
            new_sha = ""
        return True, (
            f"Pushed to origin/master ({new_sha or 'ok'}). Streamlit Cloud "
            f"will redeploy in ~1 minute; {success_label} are now durable."
        )

    if r.status_code == 409:
        return False, (
            "Conflict (409) — master moved while we were pushing. The cron "
            "may have just committed. Click Save again to retry."
        )
    return False, (
        f"GitHub API returned {r.status_code}: {r.text[:200]}"
    )


def push_flag_store_to_repo(
    commit_message: str | None = None,
    flagger_name: str = "",
) -> tuple[bool, str]:
    """Commit `data/flagged_findings.json` to origin/master."""
    suffix = f" — by {flagger_name}" if flagger_name else ""
    msg = commit_message or f"Sync flag store from cloud dashboard{suffix} [skip ci]"
    return push_file_to_repo(FLAG_FILE_PATH, msg, success_label="flags")


def push_overrides_to_repo(
    commit_message: str | None = None,
) -> tuple[bool, str]:
    """Commit `data/projections/transaction_overrides.json` to origin/master."""
    msg = commit_message or "Sync transaction dismissals from cloud dashboard [skip ci]"
    return push_file_to_repo(OVERRIDES_FILE_PATH, msg, success_label="dismissals")


# -----------------------------------------------------------------------
# Debounced auto-push for the flag store.
#
# Each visitor save writes `data/flagged_findings.json` on the ephemeral
# Streamlit Cloud container. A code push to master between cron runs
# triggers a redeploy that throws those writes away. The autosave UI
# now calls `request_flag_autopush()` after every mutation so the file
# is committed to origin/master in the background, without the visitor
# having to remember the manual "Save flags to repo" button.
#
# Constraints:
#   - Every GitHub Contents API push triggers a Streamlit Cloud
#     redeploy (~1 min). Pushing on every keystroke would thrash the
#     site. We coalesce into one push per AUTO_PUSH_THROTTLE_SECONDS.
#   - The first save in a fresh container pushes immediately so the
#     visitor's work is protected against an imminent redeploy.
#   - Subsequent saves within the throttle window arm a trailing-edge
#     timer so the latest state gets pushed once the window closes.
#   - Background threads can't read `st.secrets` (no ScriptRunContext)
#     so we resolve the PAT once on the main thread and cache it.
#   - All failures are silent best-effort: the daily 10 UTC cron and
#     the manual Save-to-repo button remain as fallbacks.
# -----------------------------------------------------------------------

AUTO_PUSH_THROTTLE_SECONDS = 60

_autopush_lock = threading.Lock()
_autopush_last_at: float = 0.0
_autopush_timer: threading.Timer | None = None
_autopush_cached_pat: str | None = None
_autopush_pat_resolved: bool = False


def _autopush_get_token() -> str | None:
    """Return the cached PAT, resolving it on first call.

    Must be called at least once from inside a ScriptRunContext (i.e.
    a Streamlit page render or callback) so st.secrets is reachable.
    After that, the cached value is safe to read from any thread.
    """
    global _autopush_cached_pat, _autopush_pat_resolved
    if _autopush_pat_resolved:
        return _autopush_cached_pat
    try:
        _autopush_cached_pat = _get_pat()
    except Exception:
        _autopush_cached_pat = None
    _autopush_pat_resolved = True
    return _autopush_cached_pat


def request_flag_autopush() -> None:
    """Schedule a debounced auto-push of the flag store.

    Cold-start (first call in this container): push immediately in a
    background thread. Within the throttle window: arm a trailing-edge
    timer so the most-recent state still lands without thrashing
    redeploys. No-op when no PAT is configured (local dev or missing
    GITHUB_PAT secret).
    """
    token = _autopush_get_token()
    if not token:
        return

    global _autopush_last_at, _autopush_timer
    now = time.time()
    with _autopush_lock:
        elapsed = now - _autopush_last_at
        cold_start = _autopush_last_at == 0.0
        if cold_start or elapsed >= AUTO_PUSH_THROTTLE_SECONDS:
            _autopush_last_at = now
            threading.Thread(
                target=_autopush_run, args=(token,), daemon=True,
            ).start()
        else:
            # Inside throttle window. Arm a single deferred flush if
            # one isn't already in flight, so the most recent save
            # gets pushed once the window closes.
            if _autopush_timer is None or not _autopush_timer.is_alive():
                delay = max(1.0, AUTO_PUSH_THROTTLE_SECONDS - elapsed)
                _autopush_timer = threading.Timer(
                    delay, _autopush_timer_fired, args=(token,),
                )
                _autopush_timer.daemon = True
                _autopush_timer.start()


def _autopush_timer_fired(token: str) -> None:
    global _autopush_last_at, _autopush_timer
    with _autopush_lock:
        _autopush_last_at = time.time()
        _autopush_timer = None
    _autopush_run(token)


def _autopush_run(token: str) -> None:
    try:
        _autopush_push_with_token(token)
    except Exception:
        # Best-effort. Failures fall back to the daily 10 UTC cron and
        # the manual Save-flags-to-repo button on the Flagged page.
        pass


def _autopush_push_with_token(token: str) -> None:
    """Inline copy of `push_file_to_repo` that takes the token directly
    so background threads don't need ScriptRunContext for `st.secrets`."""
    project_root = Path(__file__).parent.parent
    local_path = project_root / FLAG_FILE_PATH
    if not local_path.exists():
        return

    content_b64 = base64.b64encode(local_path.read_bytes()).decode("ascii")

    import requests

    url = f"{_GITHUB_API}/repos/{REPO_OWNER}/{REPO_NAME}/contents/{FLAG_FILE_PATH}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    sha: str | None = None
    r = requests.get(url, headers=headers, params={"ref": BRANCH}, timeout=15)
    if r.status_code == 200:
        try:
            sha = r.json().get("sha")
        except Exception:
            sha = None
    elif r.status_code != 404:
        # Auth, permission, or transient — bail; fallbacks will catch us.
        return

    payload = {
        "message": "Auto-sync visitor flag from cloud dashboard [skip ci]",
        "content": content_b64,
        "branch": BRANCH,
    }
    if sha:
        payload["sha"] = sha
    requests.put(url, headers=headers, json=payload, timeout=20)
