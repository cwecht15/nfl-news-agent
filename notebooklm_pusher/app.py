"""NotebookLM Team Pusher — standalone interface.

A small, self-contained Streamlit app (separate from the main dashboard) for
pushing locally-collected YouTube transcripts into the correct per-team
NotebookLM notebook.

Flow:
  pick team -> detect the newest source already in that team's notebook
  (the "last added" cutoff) -> list local videos newer than that -> you check
  which to push -> push to that team's notebook via the NotebookLM HTTP server.

Requirements:
  - Node.js 20+ on PATH — the app starts the NotebookLM engine itself
    (tools/notebooklm-mcp via stdio; NO HTTP server, no :3000). Auth is shared
    with Claude Code's server; if never set up: cd tools/notebooklm-mcp && npm run setup-auth
  - config/notebooklm_notebooks.json   team-abbr -> notebook URL
  - config/teams.yaml                  team names + official channel handle
  - data/raw/<date>/youtube.json       locally collected transcripts

Run:  Launch_NotebookLM_Pusher.bat     (or: streamlit run notebooklm_pusher/app.py)
"""

from __future__ import annotations

import datetime as dt
import glob
import json
import os
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mcp_client import MCPError, NotebookLMClient  # noqa: E402

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

# ──────────────────────────────────────────────────────────
# Paths & constants
# ──────────────────────────────────────────────────────────

ROOT = Path(__file__).resolve().parent.parent
REGISTRY_PATH = ROOT / "config" / "notebooklm_notebooks.json"
TEAMS_YAML = ROOT / "config" / "teams.yaml"
YOUTUBE_GLOB = str(ROOT / "data" / "raw" / "*" / "youtube.json")
PUSHED_PATH = ROOT / "data" / "notebooklm_pushed.json"
NB_CACHE_PATH = ROOT / "data" / "notebooklm_notebook_cache.json"
PENDING_PATH = ROOT / "data" / "notebooklm_pending.json"

SOURCE_CAP_FREE = 50           # free-tier sources/notebook
PUSH_THROTTLE_SEC = 1.5        # pause between pushes to be gentle on the server
MATCH_THRESHOLD = 0.5          # jaccard for matching notebook source -> local video

st.set_page_config(page_title="NotebookLM Team Pusher", page_icon="📕", layout="wide")


# ──────────────────────────────────────────────────────────
# NotebookLM client — spawns the notebooklm-mcp stdio server
# (dist/index.js, same one Claude Code uses) and talks JSON-RPC to
# it. Self-contained: no HTTP wrapper, no :3000, no separate window.
# ──────────────────────────────────────────────────────────

@st.cache_resource(show_spinner="Starting the NotebookLM engine…")
def get_client() -> NotebookLMClient:
    return NotebookLMClient()


def client_or_error() -> tuple[NotebookLMClient | None, str | None]:
    try:
        c = get_client()
    except MCPError as e:
        return None, str(e)
    except Exception as e:  # noqa: BLE001
        return None, f"{type(e).__name__}: {e}"
    if not c.alive():
        get_client.clear()
        return None, ("NotebookLM engine stopped (see notebooklm_pusher/mcp_server.log). "
                      "Reload the page to restart it.")
    return c, None


def nb_ping() -> tuple[bool, str]:
    c, err = client_or_error()
    if err:
        return False, err
    try:
        data = c.call_tool("get_health", {}, timeout=45).get("data", {})
    except Exception as e:  # noqa: BLE001 — never let one call crash a batch
        return False, str(e)
    return True, ("authenticated" if data.get("authenticated") else "up (not authenticated)")


def nb_list_sources(notebook_url: str) -> tuple[list[dict], int, str | None]:
    """List sources in a notebook. Drives a browser, so allow a couple minutes."""
    c, err = client_or_error()
    if err:
        return [], 0, err
    try:
        r = c.call_tool("list_content", {"notebook_url": notebook_url}, timeout=180)
    except Exception as e:  # noqa: BLE001 — never let one call crash a batch
        return [], 0, str(e)
    if not r.get("success"):
        return [], 0, str(r.get("error") or r)
    data = r.get("data", r)
    sources = data.get("sources") or []
    return sources, data.get("sourceCount", len(sources)), None


def nb_add_youtube(notebook_url: str, url: str) -> tuple[bool, str]:
    """Add a YouTube source. Treats the known verifier false-negative as ok."""
    c, err = client_or_error()
    if err:
        return False, err
    try:
        r = c.call_tool(
            "add_source",
            {"source_type": "youtube", "url": url, "notebook_url": notebook_url},
            timeout=240,
        )
    except Exception as e:  # noqa: BLE001 — never let one call crash a batch
        return False, str(e)
    if r.get("success"):
        return True, str(r.get("data", {}).get("status", "ok"))
    err_text = str(r.get("error") or r).lower()
    if "not visible in list" in err_text or "not found after upload" in err_text:
        return True, "added (verifier false-negative; assumed ok)"
    return False, str(r.get("error") or r)


def nb_delete_source(notebook_url: str, source_id: str = "",
                     source_name: str = "") -> tuple[bool, str]:
    c, err = client_or_error()
    if err:
        return False, err
    args = {"notebook_url": notebook_url}
    if source_name:
        args["source_name"] = source_name   # stable across a batch; ids can shift
    elif source_id:
        args["source_id"] = source_id
    try:
        r = c.call_tool("delete_source", args, timeout=120)
    except Exception as e:  # noqa: BLE001 — never let one call crash a batch
        return False, str(e)
    # The per-op verifier is unreliable (stale list) — callers should re-list to
    # confirm rather than trust this bool.
    return bool(r.get("success")), str(r.get("error") or "deleted")


# ──────────────────────────────────────────────────────────
# Config / data loaders
# ──────────────────────────────────────────────────────────

@st.cache_data
def load_teams() -> dict[str, dict]:
    """abbr -> {name, official_handle}. Official = first youtube channel."""
    if not TEAMS_YAML.exists() or yaml is None:
        return {}
    doc = yaml.safe_load(TEAMS_YAML.read_text(encoding="utf-8")) or {}
    out = {}
    for t in doc.get("teams", []):
        abbr = str(t.get("abbr"))
        chans = t.get("youtube_channels") or []
        handle = (chans[0].get("handle") if chans else "") or ""
        out[abbr] = {"name": t.get("name", abbr), "official_handle": handle}
    return out


@st.cache_data
def load_registry() -> dict[str, str]:
    if not REGISTRY_PATH.exists():
        return {}
    return json.loads(REGISTRY_PATH.read_text(encoding="utf-8")).get("notebooks", {})


def _raw_signature() -> tuple:
    files = glob.glob(YOUTUBE_GLOB)
    return (len(files), max((os.path.getmtime(f) for f in files), default=0.0))


@st.cache_data
def load_all_videos(_sig: tuple) -> dict[str, list[dict]]:
    """team -> [videos]; dedup by video_id keeping earliest publish date."""
    seen: dict[str, dict] = {}
    for f in glob.glob(YOUTUBE_GLOB):
        try:
            items = json.loads(Path(f).read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        if isinstance(items, dict):
            items = items.get("transcripts", list(items.values()))
        for it in items:
            if not isinstance(it, dict):
                continue
            team = it.get("team")
            vid = it.get("video_id")
            if not team or not vid or team == "NFL":
                continue
            pub = (it.get("published") or "")[:10]
            row = {
                "video_id": vid,
                "team": team,
                "title": it.get("title", ""),
                "channel": it.get("channel_name", ""),
                "published": pub,
                "url": it.get("url") or f"https://www.youtube.com/watch?v={vid}",
                "text_len": len(it.get("text") or ""),
            }
            if vid not in seen or pub < seen[vid]["published"]:
                seen[vid] = row
    by_team: dict[str, list[dict]] = {}
    for row in seen.values():
        by_team.setdefault(row["team"], []).append(row)
    for rows in by_team.values():
        rows.sort(key=lambda r: r["published"])
    return by_team


def load_pushed() -> dict[str, list[str]]:
    if not PUSHED_PATH.exists():
        return {}
    try:
        d = json.loads(PUSHED_PATH.read_text(encoding="utf-8"))
        return {k: list(v) for k, v in d.items() if isinstance(v, list)}
    except Exception:  # noqa: BLE001
        return {}


def record_pushed(notebook_url: str, video_ids: list[str]) -> None:
    if not video_ids:
        return
    hist = load_pushed()
    cur = set(hist.get(notebook_url, []))
    cur.update(video_ids)
    hist[notebook_url] = sorted(cur)
    PUSHED_PATH.parent.mkdir(parents=True, exist_ok=True)
    PUSHED_PATH.write_text(json.dumps(hist, indent=2), encoding="utf-8")


def unrecord_pushed(notebook_url: str, video_ids: list[str]) -> None:
    """Drop video_ids from push history (used when their source is deleted, so
    they re-surface as candidates instead of being permanently hidden)."""
    if not video_ids:
        return
    hist = load_pushed()
    drop = set(video_ids)
    hist[notebook_url] = [v for v in hist.get(notebook_url, []) if v not in drop]
    PUSHED_PATH.parent.mkdir(parents=True, exist_ok=True)
    PUSHED_PATH.write_text(json.dumps(hist, indent=2), encoding="utf-8")


# ──────────────────────────────────────────────────────────
# Notebook-source cache — a stored snapshot of each notebook's current sources,
# so we DON'T re-read every notebook on every run. Refreshed only when the user
# clicks Recheck (or automatically right after a push/delete on that notebook).
# ──────────────────────────────────────────────────────────

def load_nb_cache() -> dict:
    if not NB_CACHE_PATH.exists():
        return {}
    try:
        d = json.loads(NB_CACHE_PATH.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def save_nb_cache_entry(notebook_url: str, source_names: list[str], count: int,
                        checked_at: str, is_recheck: bool = False) -> None:
    """Store the live source list. `sources` is always current (drives dedup);
    `recheck_sources` is a baseline that only advances on an explicit recheck, so
    snapshot_diff() can compare the two most recent rechecks."""
    cache = load_nb_cache()
    prev = cache.get(notebook_url, {})
    entry = {"checked_at": checked_at, "count": count, "sources": source_names}
    if is_recheck:
        entry["recheck_sources"] = source_names
        entry["recheck_at"] = checked_at
        # backfill from the pre-diff cache format so the first new recheck diffs
        # against the existing snapshot rather than flagging everything as new
        entry["prev_recheck_sources"] = prev.get("recheck_sources", prev.get("sources", []))
        entry["prev_recheck_at"] = prev.get("recheck_at", prev.get("checked_at"))
    else:  # push/delete: refresh live sources, keep recheck baselines untouched
        entry["recheck_sources"] = prev.get("recheck_sources", [])
        entry["recheck_at"] = prev.get("recheck_at")
        entry["prev_recheck_sources"] = prev.get("prev_recheck_sources", [])
        entry["prev_recheck_at"] = prev.get("prev_recheck_at")
    cache[notebook_url] = entry
    NB_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    NB_CACHE_PATH.write_text(json.dumps(cache, indent=2), encoding="utf-8")


# ──────────────────────────────────────────────────────────
# Matching helpers
# ──────────────────────────────────────────────────────────

def _norm_tokens(s: str) -> set[str]:
    s = re.sub(r"\|.*$", "", s.lower())          # drop "| Team Name" suffix
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return set(w for w in s.split() if w)


def match_sources_to_videos(
    source_names: list[str], videos: list[dict]
) -> tuple[str | None, set[str]]:
    """Return (cutoff_date, approved_channels) from notebook sources.

    cutoff = newest publish date among videos that match an existing source.
    approved_channels = channel names of those matched videos.
    """
    tok = {v["video_id"]: _norm_tokens(v["title"]) for v in videos}
    matched: list[dict] = []
    for name in source_names:
        ns = _norm_tokens(name)
        if not ns:
            continue
        best = None
        for v in videos:
            b = tok[v["video_id"]]
            if not b:
                continue
            j = len(ns & b) / len(ns | b)
            if best is None or j > best[0]:
                best = (j, v)
        if best and best[0] >= MATCH_THRESHOLD:
            matched.append(best[1])
    if not matched:
        return None, set()
    cutoff = max(v["published"] for v in matched)
    channels = {v["channel"] for v in matched}
    return cutoff, channels


def video_id_for_source(name: str, videos: list[dict]) -> str | None:
    """Best-effort reverse match: a notebook source name -> local video_id."""
    ns = _norm_tokens(name)
    if not ns:
        return None
    best = None
    for v in videos:
        b = _norm_tokens(v["title"])
        if not b:
            continue
        j = len(ns & b) / len(ns | b)
        if best is None or j > best[0]:
            best = (j, v["video_id"])
    return best[1] if best and best[0] >= MATCH_THRESHOLD else None


def cached_notebook_ids(notebook_url: str, team_videos: list[dict]) -> set[str]:
    """video_ids already present in the notebook, per the cached source snapshot."""
    entry = load_nb_cache().get(notebook_url)
    if not entry:
        return set()
    out = set()
    for name in entry.get("sources", []):
        vid = video_id_for_source(name, team_videos)
        if vid:
            out.add(vid)
    return out


def det_from_cache(notebook_url: str) -> dict | None:
    """Build the per-team notebook snapshot dict from the on-disk cache."""
    entry = load_nb_cache().get(notebook_url)
    if not entry:
        return None
    names = entry.get("sources", [])
    return {"sources": [{"name": n, "id": ""} for n in names],
            "count": entry.get("count", len(names)),
            "checked_at": entry.get("checked_at", "?"),
            "cached": True}


def snapshot_diff(notebook_url: str) -> dict | None:
    """Source names added/removed between the two most recent rechecks.

    Returns None if there's no prior recheck to compare against (first snapshot).
    """
    e = load_nb_cache().get(notebook_url)
    if not e or e.get("prev_recheck_at") is None:
        return None
    cur, prev = e.get("recheck_sources", []), e.get("prev_recheck_sources", [])
    curset, prevset = set(cur), set(prev)
    return {"added": [n for n in cur if n not in prevset],
            "removed": [n for n in prev if n not in curset],
            "prev_checked_at": e.get("prev_recheck_at"),
            "checked_at": e.get("recheck_at")}


# ──────────────────────────────────────────────────────────
# Pending queue — videos submitted to a notebook but not yet confirmed present.
# NotebookLM's YouTube ingestion lags (minutes), so we DON'T trust an immediate
# re-read; we mark submissions pending, exclude them from candidates (no dupes),
# and reconcile on the next Recheck: landed -> push history, rest stay pending.
# ──────────────────────────────────────────────────────────

def load_pending() -> dict:
    if not PENDING_PATH.exists():
        return {}
    try:
        d = json.loads(PENDING_PATH.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _write_pending(p: dict) -> None:
    PENDING_PATH.parent.mkdir(parents=True, exist_ok=True)
    PENDING_PATH.write_text(json.dumps(p, indent=2), encoding="utf-8")


def add_pending(notebook_url: str, items: list[dict]) -> None:
    """items: [{'video_id','title'}]. Stamps submitted_at, dedups by video_id."""
    if not items:
        return
    p = load_pending()
    cur = {it["video_id"]: it for it in p.get(notebook_url, [])}
    now = dt.datetime.now().isoformat(timespec="minutes")
    for it in items:
        cur[it["video_id"]] = {"video_id": it["video_id"], "title": it["title"],
                               "submitted_at": now}
    p[notebook_url] = list(cur.values())
    _write_pending(p)


def pending_ids(notebook_url: str) -> set[str]:
    return {it["video_id"] for it in load_pending().get(notebook_url, [])}


def clear_pending(notebook_url: str, video_ids: list[str]) -> None:
    p = load_pending()
    drop = set(video_ids)
    remaining = [it for it in p.get(notebook_url, []) if it["video_id"] not in drop]
    if remaining:
        p[notebook_url] = remaining
    else:
        p.pop(notebook_url, None)
    _write_pending(p)


def reconcile_pending(notebook_url: str, fresh_source_names: list[str]) -> tuple[int, int]:
    """Landed pending -> push history; keep the rest. Returns (confirmed, still_pending)."""
    pend = load_pending().get(notebook_url, [])
    if not pend:
        return 0, 0
    src_tok = [_norm_tokens(n) for n in fresh_source_names]

    def landed(title: str) -> bool:
        a = _norm_tokens(title)
        return bool(a) and any(
            tb and len(a & tb) / len(a | tb) >= MATCH_THRESHOLD for tb in src_tok)

    confirmed = [it["video_id"] for it in pend if landed(it["title"])]
    if confirmed:
        record_pushed(notebook_url, confirmed)
        clear_pending(notebook_url, confirmed)
    return len(confirmed), len(pend) - len(confirmed)


def is_officialish(channel: str, team_name: str, handle: str) -> bool:
    """Best-effort 'official team channel' guess for the pre-Detect default.

    Deliberately tight (exact team-name match, or a dedicated press-conference
    channel) so fan channels that merely contain the nickname — 'Steelers
    Collective', 'Steelers Blitz' — don't sneak into the default. After Detect
    runs, the notebook's own channels override this anyway.
    """
    c = channel.strip().lower()
    if "press conference" in c or "presser" in c:
        return True
    return c == team_name.strip().lower()


# ──────────────────────────────────────────────────────────
# UI
# ──────────────────────────────────────────────────────────

st.title("📕 NotebookLM Team Pusher")
st.caption(
    "Push locally-collected YouTube transcripts into each team's NotebookLM "
    "notebook — only the videos newer than what's already in there."
)

teams = load_teams()
registry = load_registry()
videos_by_team = load_all_videos(_raw_signature())

up, ping = nb_ping()
c1, c2 = st.columns([3, 2])
with c1:
    if not up:
        st.error(
            "NotebookLM engine unavailable — " + ping + "  \n"
            "Needs Node 20+ and the built server under `tools/notebooklm-mcp`. "
            "You can still browse/select; pushing needs the engine."
        )
    elif "not authenticated" in ping:
        st.warning(
            "Engine running but **not authenticated** — run "
            "`cd tools/notebooklm-mcp && npm run setup-auth`, then reload."
        )
    else:
        st.success(f"NotebookLM engine: {ping} — embedded (no :3000)")
with c2:
    if not registry:
        st.error("config/notebooklm_notebooks.json missing or empty.")
    else:
        st.caption(f"{len(registry)} team notebooks mapped · {len(videos_by_team)} teams with local videos")

if not registry or not teams:
    st.stop()

mode = st.radio("Mode", ["Single team", "All teams"], horizontal=True,
                key="mode", label_visibility="collapsed")
st.divider()


def _official_channels(ab: str) -> list[str]:
    """Channels for this team's local videos that look like the official channel."""
    tv = videos_by_team.get(ab, [])
    return sorted({c for c in {v["channel"] for v in tv}
                   if is_officialish(c, teams[ab]["name"], teams[ab]["official_handle"])})


# ══════════════════════════════════════════════════════════
# ALL TEAMS — pick a date, dedup off cached snapshots, review, push
# ══════════════════════════════════════════════════════════
if mode == "All teams":
    all_abbrs = sorted([a for a in registry if a in teams], key=lambda a: teams[a]["name"])
    st.subheader("All teams — pick a date → review → push")
    st.caption(
        f"{len(all_abbrs)} teams. Policy: **official channel, every video published on/after your "
        "date, minus what's already in the notebook or already pushed.** Notebook contents come "
        "from a stored snapshot — reading all notebooks fresh is a separate ~20–35 min 'Recheck' "
        "you run only when you want."
    )
    cache = load_nb_cache()
    cached_n = sum(1 for ab in all_abbrs if registry[ab] in cache)

    ac = st.columns([2, 2, 3])
    with ac[0]:
        all_since = st.date_input("Videos published on/after",
                                  value=dt.date.today() - dt.timedelta(days=30),
                                  key="all_since")
    with ac[1]:
        recheck_all = st.button(f"🔄 Recheck all ({len(all_abbrs)})", disabled=not up,
                                help="Reads every notebook (~20–35 min) to refresh the dedup snapshots.")
    with ac[2]:
        st.caption(f"{cached_n}/{len(all_abbrs)} notebooks have a stored snapshot"
                   + ("." if cached_n == len(all_abbrs)
                      else " — uncached teams dedup on push-history only."))

    if recheck_all:
        prog = st.progress(0.0, text="Reading notebooks…")
        today_iso = dt.date.today().isoformat()
        fails = []
        reconciled = 0
        for i, ab in enumerate(all_abbrs, 1):
            prog.progress(i / len(all_abbrs),
                          text=f"Reading {i}/{len(all_abbrs)}: {teams[ab]['name']}")
            sources, count, err = nb_list_sources(registry[ab])
            if err:
                fails.append(ab)
                continue
            names = [s.get("name", "") for s in sources]
            save_nb_cache_entry(registry[ab], names, count, today_iso, is_recheck=True)
            c, _ = reconcile_pending(registry[ab], names)
            reconciled += c
        cache = load_nb_cache()
        cached_n = sum(1 for ab in all_abbrs if registry[ab] in cache)
        (st.warning if fails else st.success)(
            f"Rechecked all notebooks. {reconciled} pending confirmed."
            + (f" {len(fails)} failed: " + ", ".join(teams[a]['name'] for a in fails)
               if fails else ""))
        changes = []
        for ab in all_abbrs:
            d = snapshot_diff(registry[ab])
            if d and (d["added"] or d["removed"]):
                changes.append({"team": teams[ab]["name"], "added": len(d["added"]),
                                "removed": len(d["removed"]),
                                "detail": "; ".join(["+ " + n[:40] for n in d["added"]]
                                                    + ["− " + n[:40] for n in d["removed"]])[:200]})
        if changes:
            with st.expander(f"🔁 {len(changes)} notebook(s) changed since previous recheck",
                             expanded=True):
                st.dataframe(pd.DataFrame(changes).sort_values(["added", "removed"],
                             ascending=False), hide_index=True, width="stretch")
        else:
            st.caption("No changes vs the previous recheck (or this was the first snapshot).")

    since_iso = all_since.isoformat()
    rows, caprows = [], []
    for ab in all_abbrs:
        url = registry[ab]
        tv = videos_by_team.get(ab, [])
        offi = _official_channels(ab)
        pend = pending_ids(url)
        dedup = set(load_pushed().get(url, [])) | cached_notebook_ids(url, tv) | pend
        cands = sorted([v for v in tv if v["published"] >= since_iso
                        and v["channel"] in offi and v["video_id"] not in dedup],
                       key=lambda v: v["published"])
        entry = cache.get(url)
        cur = entry["count"] if entry else None
        eff = (cur or 0) + len(pend)
        caprows.append({"team": teams[ab]["name"],
                        "in notebook": cur if cur is not None else "—",
                        "pending": len(pend),
                        "new": len(cands),
                        "projected": (eff + len(cands)) if cur is not None else "—",
                        "over 50": "⚠" if (cur is not None and eff + len(cands) > SOURCE_CAP_FREE) else "",
                        "snapshot": entry["checked_at"] if entry else "—"})
        for v in cands:
            rows.append({"push": True, "team": ab, "date": v["published"],
                         "channel": v["channel"], "title": v["title"],
                         "url": v["url"], "video_id": v["video_id"]})

    st.markdown(f"**{len(rows)} candidate videos** across {len({x['team'] for x in rows})} teams "
                f"(official channel · on/after {since_iso}).")
    if caprows:
        with st.expander("Per-team summary (counts · cap · snapshot date)", expanded=True):
            st.dataframe(pd.DataFrame(caprows).sort_values("new", ascending=False),
                         hide_index=True, width="stretch")
            if cached_n < len(all_abbrs):
                st.info("Teams showing '—' have no snapshot, so they dedup only against push "
                        "history and may list videos already in the notebook. Recheck to fix.")

    if not rows:
        st.info("No candidates for this date. Move the date earlier, or Recheck if snapshots look stale.")
        st.stop()

    anonce = st.session_state.setdefault("anonce", 0)
    bc = st.columns([1, 1, 5])
    with bc[0]:
        if st.button("Select all"):
            st.session_state["aseldef"] = True; st.session_state["anonce"] += 1; st.rerun()
    with bc[1]:
        if st.button("Clear"):
            st.session_state["aseldef"] = False; st.session_state["anonce"] += 1; st.rerun()
    seldef = st.session_state.get("aseldef", True)
    for x in rows:
        x["push"] = seldef

    aedit = st.data_editor(
        pd.DataFrame(rows),
        key=f"all_editor_{anonce}",
        hide_index=True, width="stretch",
        column_config={
            "push": st.column_config.CheckboxColumn("✓", width="small"),
            "team": st.column_config.TextColumn("Team", width="small"),
            "date": st.column_config.TextColumn("Date", width="small"),
            "channel": st.column_config.TextColumn("Channel", width="medium"),
            "title": st.column_config.TextColumn("Title", width="large"),
            "url": st.column_config.LinkColumn("Video", display_text="▶", width="small"),
            "video_id": None,
        },
        disabled=["team", "date", "channel", "title", "url"],
    )
    sel = aedit[aedit["push"]].to_dict("records") if "push" in aedit else []

    st.divider()
    pc = st.columns([2, 5])
    with pc[0]:
        run_push = st.button(f"📕 Push {len(sel)} across all teams",
                             type="primary", disabled=not sel or not up)
    with pc[1]:
        st.caption("Submits team-by-team, caps each notebook at 50 (current + pending), marks all "
                   "pending — run Recheck all afterward to reconcile. Safe to re-run.")

    if run_push and sel:
        bteam: dict = defaultdict(list)
        for r in sel:
            bteam[r["team"]].append(r)
        overall = st.progress(0.0, text="Starting…")
        done, total = 0, len(sel)
        report = []
        for ab, items in bteam.items():
            url = registry[ab]
            entry = cache.get(url)
            cur = (entry["count"] if entry else 0) + len(pending_ids(url))
            room = max(0, SOURCE_CAP_FREE - cur) if entry else len(items)
            items.sort(key=lambda r: r["date"])           # oldest first for cap fairness
            to_push, capped = items[:room], items[room:]
            for r in to_push:
                nb_add_youtube(url, r["url"])
                done += 1
                overall.progress(min(done / total, 1.0),
                                 text=f"{teams[ab]['name']}: submitting ({done}/{total})…")
                time.sleep(PUSH_THROTTLE_SEC)
            add_pending(url, [{"video_id": r["video_id"], "title": r["title"]} for r in to_push])
            report.append({"team": teams[ab]["name"], "submitted": len(to_push),
                           "skipped (cap)": len(capped)})
        overall.progress(1.0, text="Done.")
        st.success("Submitted across all teams — all **pending**. NotebookLM ingests slowly, so run "
                   "**Recheck all** in a few minutes to reconcile (landed → push history, rest stay "
                   "pending).")
        st.dataframe(pd.DataFrame(report), hide_index=True, width="stretch")
        st.caption("submitted = sent to NotebookLM (pending confirmation) · skipped (cap) = would "
                   "exceed 50 sources counting current + pending.")
    st.stop()


# --- Team picker -------------------------------------------------------------

abbrs = [a for a in registry if a in teams]
abbrs.sort(key=lambda a: teams[a]["name"])
team = st.selectbox(
    "Team",
    abbrs,
    format_func=lambda a: f"{teams[a]['name']}  ({a})",
    key="team",
)
notebook_url = registry[team]
tinfo = teams[team]
team_videos = videos_by_team.get(team, [])

hc = st.columns([3, 2, 2])
with hc[0]:
    st.markdown(f"**Notebook:** [{notebook_url.split('/')[-1][:8]}…]({notebook_url})")
with hc[1]:
    st.caption(f"Official channel: `{tinfo['official_handle']}`")
with hc[2]:
    st.caption(f"{len(team_videos)} local videos on file")

# --- Per-team notebook snapshot (from cache; refresh via Recheck) ------------

state = st.session_state.setdefault("detect", {})  # team -> snapshot dict
det = state.get(team)
if det is None:
    det = det_from_cache(notebook_url)   # load stored snapshot from disk if present
    if det:
        state[team] = det

dc = st.columns([2, 3])
with dc[0]:
    if st.button("🔄 Recheck notebook (read sources)",
                 type="secondary" if det else "primary", disabled=not up):
        with st.spinner("Reading the notebook's current sources…"):
            sources, count, err = nb_list_sources(notebook_url)
        if err:
            st.error(f"Couldn't read notebook: {err}")
        else:
            today_iso = dt.date.today().isoformat()
            names = [s.get("name", "") for s in sources]
            save_nb_cache_entry(notebook_url, names, count, today_iso, is_recheck=True)
            conf, still = reconcile_pending(notebook_url, names)
            state[team] = {"sources": sources, "count": count,
                           "checked_at": today_iso, "cached": False}
            det = state[team]
            msg = f"Read {count} sources — snapshot stored."
            if conf or still:
                msg += f" Pending reconciled: {conf} confirmed, {still} still awaiting."
            st.success(msg)
with dc[1]:
    if det:
        st.caption(f"Notebook snapshot: **{det['count']}/{SOURCE_CAP_FREE}** sources "
                   f"(checked {det.get('checked_at', '?')}). Dedup excludes these.")
    else:
        st.caption("No stored snapshot yet — **Recheck** once to also skip videos already in the "
                   "notebook. Until then, dedup uses this tool's push history only.")

_diff = snapshot_diff(notebook_url)
if _diff and (_diff["added"] or _diff["removed"]):
    with st.expander(f"🔁 Changed since previous recheck ({_diff['prev_checked_at']} → "
                     f"{_diff['checked_at']}): +{len(_diff['added'])} / −{len(_diff['removed'])}",
                     expanded=True):
        if _diff["added"]:
            st.markdown("**Added**")
            for n in _diff["added"]:
                st.markdown(f"- 🟢 {n}")
        if _diff["removed"]:
            st.markdown("**Removed**")
            for n in _diff["removed"]:
                st.markdown(f"- 🔴 {n}")

_pend = load_pending().get(notebook_url, [])
if _pend:
    with st.expander(f"⏳ {len(_pend)} awaiting confirmation for {tinfo['name']} "
                     "(submitted, not yet seen in the notebook)", expanded=True):
        for it in sorted(_pend, key=lambda x: x.get("submitted_at", "")):
            st.markdown(f"- {it['title'][:64]}  ·  _{it.get('submitted_at', '')}_")
        st.caption("A **Recheck** reconciles these — landed ones move to push history automatically.")
        if st.button("Clear pending (treat as failed → re-list as candidates)", key=f"clrp_{team}"):
            clear_pending(notebook_url, [it["video_id"] for it in _pend])
            st.rerun()

# --- Filters -----------------------------------------------------------------

all_channels = sorted({v["channel"] for v in team_videos})
default_channels = [c for c in all_channels
                    if is_officialish(c, tinfo["name"], tinfo["official_handle"])] or all_channels

fc = st.columns([2, 3])
with fc[0]:
    since_date = st.date_input(
        "Look for videos published on/after",
        value=dt.date.today() - dt.timedelta(days=30),
        key=f"since_{team}",
        help="You pick the window — change it anytime; your choice sticks for this team.",
    )
with fc[1]:
    chosen_channels = st.multiselect(
        "Channels",
        all_channels,
        default=[c for c in default_channels if c in all_channels],
        key=f"chan_{team}",
        help="Defaults to the official team channel; add beat/fan channels if you want them.",
    )

# --- Candidate computation ---------------------------------------------------

pushed_ids = set(load_pushed().get(notebook_url, []))
in_notebook = cached_notebook_ids(notebook_url, team_videos)
pend_ids = pending_ids(notebook_url)
dedup = pushed_ids | in_notebook | pend_ids
since_iso = since_date.isoformat()
cands = [
    v for v in team_videos
    if v["published"] >= since_iso
    and v["channel"] in chosen_channels
    and v["video_id"] not in dedup
]
cands.sort(key=lambda v: v["published"], reverse=True)

st.divider()
st.subheader(f"Candidates to push — {len(cands)}")
st.caption(
    f"On/after {since_iso} · {len(chosen_channels)} channel(s) · excluding "
    f"{len(pushed_ids)} pushed"
    + (f" + {len(in_notebook)} in notebook" if in_notebook else "")
    + (f" + {len(pend_ids)} pending" if pend_ids else "")
    + ("" if in_notebook else " (recheck to also skip what's already in the notebook)")
)

if not cands:
    st.info("No unpushed candidates for these filters. Adjust the cutoff or channels above.")
    st.stop()

# select-all / clear via a nonce that re-inits the editor
nonce_key = f"nonce_{team}"
seldef_key = f"seldef_{team}"
st.session_state.setdefault(nonce_key, 0)
st.session_state.setdefault(seldef_key, False)

bc = st.columns([1, 1, 1, 4])
with bc[0]:
    if st.button("Select all"):
        st.session_state[seldef_key] = True
        st.session_state[nonce_key] += 1
        st.rerun()
with bc[1]:
    if st.button("Clear"):
        st.session_state[seldef_key] = False
        st.session_state[nonce_key] += 1
        st.rerun()
with bc[2]:
    st.caption(f"{len(cands)} rows")

import pandas as pd

df = pd.DataFrame(
    [
        {
            "push": st.session_state[seldef_key],
            "date": v["published"],
            "channel": v["channel"],
            "title": v["title"],
            "url": v["url"],
            "video_id": v["video_id"],
        }
        for v in cands
    ]
)

edited = st.data_editor(
    df,
    key=f"editor_{team}_{st.session_state[nonce_key]}",
    hide_index=True,
    width="stretch",
    column_config={
        "push": st.column_config.CheckboxColumn("✓", width="small"),
        "date": st.column_config.TextColumn("Date", width="small"),
        "channel": st.column_config.TextColumn("Channel", width="medium"),
        "title": st.column_config.TextColumn("Title", width="large"),
        "url": st.column_config.LinkColumn("Video", display_text="▶", width="small"),
        "video_id": None,
    },
    disabled=["date", "channel", "title", "url"],
)

selected = edited[edited["push"]] if "push" in edited else edited.iloc[0:0]
sel_rows = selected.to_dict("records")

# --- Push footer -------------------------------------------------------------

st.divider()
count_known = det["count"] if det else None
projected = (count_known + len(sel_rows)) if count_known is not None else None
over_cap = projected is not None and projected > SOURCE_CAP_FREE

pc = st.columns([2, 3, 3])
with pc[0]:
    with st.popover(f"📋 Copy {len(sel_rows)} URLs", disabled=not sel_rows):
        st.code("\n".join(r["url"] for r in sel_rows) or "—", language=None)
with pc[1]:
    if projected is not None:
        (st.error if over_cap else st.caption)(
            f"Notebook would go {count_known} → **{projected}** / {SOURCE_CAP_FREE} sources"
            + ("  ⚠ over the free cap — drop some or use a paid plan." if over_cap else "")
        )
    else:
        st.caption("Recheck the notebook to see the exact source-cap math.")
with pc[2]:
    confirm = True
    if over_cap:
        confirm = st.checkbox("Push anyway (I'm on a paid plan)", key=f"ovr_{team}")
    do_push = st.button(
        f"📕 Push {len(sel_rows)} to {tinfo['name']}",
        type="primary",
        disabled=not sel_rows or not up or (over_cap and not confirm),
    )

if do_push and sel_rows:
    prog = st.progress(0.0, text=f"Submitting 0/{len(sel_rows)}…")
    for i, r in enumerate(sel_rows, 1):
        nb_add_youtube(notebook_url, r["url"])
        prog.progress(i / len(sel_rows),
                      text=f"Submitting {i}/{len(sel_rows)} — {r['title'][:48]}")
        time.sleep(PUSH_THROTTLE_SEC)
    add_pending(notebook_url,
                [{"video_id": r["video_id"], "title": r["title"]} for r in sel_rows])
    st.success(
        f"Submitted {len(sel_rows)} to {tinfo['name']} — marked **pending**. NotebookLM ingests "
        "YouTube slowly (often minutes), so they may not show up right away. Click **Recheck** in a "
        "few minutes: landed ones move to your push history, the rest stay pending (never re-listed "
        "as candidates until confirmed or cleared)."
    )
    st.session_state[nonce_key] += 1

# --- Existing sources (view / multi-delete) ----------------------------------

if det and det.get("sources"):
    with st.expander(f"📄 {det['count']} sources in this notebook — check any to remove"):
        dnonce = st.session_state.get(f"dnonce_{team}", 0)
        _vid_pub = {v["video_id"]: v["published"] for v in team_videos}

        def _src_date(name: str) -> str:
            vid = video_id_for_source(name, team_videos)
            return _vid_pub.get(vid, "") if vid else ""

        drows = [{"remove": False,
                  "date": _src_date(s.get("name", "")),
                  "source": s.get("name", "(unnamed)"),
                  "id": s.get("id", "")}
                 for s in det["sources"]]
        drows.sort(key=lambda r: r["date"], reverse=True)  # newest first; undated last
        ddf = pd.DataFrame(drows)
        dedit = st.data_editor(
            ddf,
            key=f"del_editor_{team}_{det['count']}_{dnonce}",
            hide_index=True,
            width="stretch",
            column_config={
                "remove": st.column_config.CheckboxColumn("🗑", width="small"),
                "date": st.column_config.TextColumn("Date", width="small"),
                "source": st.column_config.TextColumn("Source", width="large"),
                "id": None,
            },
            disabled=["date", "source"],
        )
        to_del = dedit[dedit["remove"]].to_dict("records") if "remove" in dedit else []
        dcols = st.columns([2, 5])
        with dcols[0]:
            do_del = st.button(
                f"🗑 Delete {len(to_del)} selected",
                disabled=not to_del or not up,
                key=f"delbtn_{team}",
            )
        with dcols[1]:
            if to_del:
                st.caption("Deletes each, then re-reads the notebook to confirm what "
                           "actually got removed (the per-delete check is unreliable).")

        if do_del and to_del:
            dprog = st.progress(0.0, text=f"Deleting 0/{len(to_del)}…")
            for i, r in enumerate(to_del, 1):
                nb_delete_source(notebook_url, source_name=r["source"])
                dprog.progress(i / len(to_del),
                               text=f"Deleting {i}/{len(to_del)} — {r['source'][:44]}")
                time.sleep(PUSH_THROTTLE_SEC)
            with st.spinner("Re-reading notebook to confirm deletions…"):
                sources2, count2, err2 = nb_list_sources(notebook_url)
            if err2:
                st.warning(f"Deletes were sent but I couldn't refresh ({err2}). Recheck the notebook.")
            else:
                remaining = {s.get("name", "") for s in sources2}
                gone = [r for r in to_del if r["source"] not in remaining]
                still = [r for r in to_del if r["source"] in remaining]
                # keep push-history honest: a removed source becomes a candidate again
                removed_vids = [v for r in gone
                                if (v := video_id_for_source(r["source"], team_videos))]
                unrecord_pushed(notebook_url, removed_vids)
                _today = dt.date.today().isoformat()
                save_nb_cache_entry(notebook_url, [s.get("name", "") for s in sources2],
                                    count2, _today)
                state[team] = {"sources": sources2, "count": count2,
                               "checked_at": _today, "cached": False}
                st.session_state[f"dnonce_{team}"] = dnonce + 1
                st.success(f"Removed {len(gone)}/{len(to_del)}. Notebook now has {count2} sources.")
                if still:
                    st.warning("Still present (delete didn't take): "
                               + "; ".join(r["source"][:40] for r in still))
                st.rerun()
