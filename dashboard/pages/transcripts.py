"""Transcripts & YouTube Links — Browse videos, download transcripts for NotebookLM."""

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import streamlit as st
from config_loader import get_data_dir, get_teams

st.header("YouTube Videos & Transcripts")
st.markdown("YouTube links for NotebookLM upload + downloadable transcripts.")

# Get team info
teams = get_teams()
team_names = {t["abbr"]: t["name"] for t in teams}

# List available dates
transcripts_base = get_data_dir("transcripts")
raw_base = get_data_dir("raw")

# All transcript directories (daily + backfill)
all_transcript_dirs = sorted(
    [d.name for d in transcripts_base.iterdir() if d.is_dir()],
    reverse=True,
)
# Daily dirs only (YYYY-MM-DD format)
daily_dirs = [d for d in all_transcript_dirs if len(d) == 10 and d[4] == "-"]
# Backfill dirs
backfill_dirs = [d for d in all_transcript_dirs if d.startswith("backfill-")]

if not all_transcript_dirs:
    st.warning(
        "No transcripts available yet. Run `python scripts/run_daily.py` "
        "to collect videos."
    )
    st.stop()


def _parse_date_safe(s: str):
    """Try to parse YYYY-MM-DD, return None on failure."""
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None


def _load_youtube_meta(dir_name: str) -> dict:
    """Load YouTube metadata for a given directory."""
    meta = {}
    for json_name in ("youtube.json", "all_transcripts.json", "all_videos.json"):
        path = raw_base / dir_name / json_name
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    for item in json.load(f):
                        vid_id = item.get("video_id", "")
                        if vid_id and vid_id not in meta:
                            meta[vid_id] = item
            except (json.JSONDecodeError, TypeError):
                continue
    return meta


def _video_date(item: dict):
    """Extract a date from a video metadata item."""
    # Try upload_date (YYYYMMDD)
    upload = str(item.get("upload_date", "") or "").strip()
    if upload:
        try:
            return datetime.strptime(upload, "%Y%m%d").date()
        except ValueError:
            pass
    # Try published_at (ISO)
    pub = str(item.get("published_at", "") or "").strip()
    if pub:
        try:
            return datetime.fromisoformat(pub).date()
        except ValueError:
            pass
    return None


def _get_transcript_files(dir_name: str) -> list[Path]:
    """Get transcript text files for a directory."""
    d = transcripts_base / dir_name
    if d.exists():
        return sorted(d.glob("*.txt"))
    return []


def _all_known_dates() -> tuple:
    """Compute the earliest and latest dates across all data (daily + backfill)."""
    dates = []
    for d in daily_dirs:
        parsed = _parse_date_safe(d)
        if parsed:
            dates.append(parsed)
    # Parse backfill ranges
    for d in backfill_dirs:
        import re as _re
        m = _re.search(r"(\d{4}-\d{2}-\d{2})-to-(\d{4}-\d{2}-\d{2})", d)
        if m:
            start = _parse_date_safe(m.group(1))
            end = _parse_date_safe(m.group(2))
            if start:
                dates.append(start)
            if end:
                dates.append(end)
    if not dates:
        today = datetime.now().date()
        return today - timedelta(days=30), today
    return min(dates), max(dates)


# ══════════════════════════════════════════════════════════
# NotebookLM Export — pick team + date range, get all URLs
# ══════════════════════════════════════════════════════════
st.subheader("NotebookLM Export")
st.markdown("Select a team and date range to get all YouTube URLs for easy upload.")

min_date, max_date = _all_known_dates()

export_cols = st.columns([2, 1, 1])
with export_cols[0]:
    export_team = st.selectbox(
        "Team",
        ["All Teams"] + sorted(team_names.keys()),
        format_func=lambda x: f"{x} — {team_names[x]}" if x in team_names else x,
        key="export_team",
    )
with export_cols[1]:
    default_start = max(min_date, max_date - timedelta(days=7))
    start_date = st.date_input("From", value=default_start, min_value=min_date, max_value=max_date, key="export_start")
with export_cols[2]:
    end_date = st.date_input("To", value=max_date, min_value=min_date, max_value=max_date, key="export_end")

# Gather URLs across the date range from ALL directories (daily + backfill)
export_urls = []
export_details = []
seen_vid_ids = set()

for dir_name in all_transcript_dirs:
    meta = _load_youtube_meta(dir_name)
    for vid_id, item in meta.items():
        if vid_id in seen_vid_ids:
            continue

        # Filter by team
        team = item.get("team", "")
        if export_team != "All Teams" and team != export_team:
            continue

        # Filter by date — use the video's own upload date
        v_date = _video_date(item)
        if v_date is None:
            # For daily dirs, use the dir date as fallback
            v_date = _parse_date_safe(dir_name)
        if v_date is None:
            continue
        if v_date < start_date or v_date > end_date:
            continue

        url = item.get("url", "")
        if not url:
            continue

        seen_vid_ids.add(vid_id)
        export_urls.append(url)
        export_details.append({
            "date": str(v_date),
            "team": team,
            "title": item.get("title", "Unknown"),
            "url": url,
            "method": item.get("method", ""),
        })

# Sort by date
export_details.sort(key=lambda x: x["date"])
export_urls = [d["url"] for d in export_details]

if export_details:
    team_label = team_names.get(export_team, export_team) if export_team != "All Teams" else "All Teams"
    st.success(f"{len(export_details)} videos for {team_label} ({start_date} to {end_date})")

    # Show details
    for detail in export_details:
        team_label = team_names.get(detail["team"], detail["team"])
        st.markdown(
            f"- **{team_label}** ({detail['date']}) — {detail['title']}"
        )

    # Copyable URL block
    st.markdown("**All URLs (copy & paste into NotebookLM):**")
    st.code("\n".join(export_urls), language=None)
else:
    if export_team != "All Teams":
        st.info(f"No videos found for {team_names.get(export_team, export_team)} in this date range.")
    else:
        st.info("No videos found in this date range.")

# ══════════════════════════════════════════════════════════
# Backfill YouTube — scan a date range for new transcripts
# ══════════════════════════════════════════════════════════
st.divider()
st.subheader("Backfill YouTube Transcripts")
st.markdown("Scan team YouTube channels for press conferences in a custom date range.")

backfill_cols = st.columns([1, 1, 1])
with backfill_cols[0]:
    bf_start = st.date_input(
        "Start date",
        value=datetime.now().date() - timedelta(days=7),
        key="bf_start",
    )
with backfill_cols[1]:
    bf_end = st.date_input(
        "End date",
        value=datetime.now().date(),
        key="bf_end",
    )
with backfill_cols[2]:
    bf_team_options = ["All Teams"] + sorted(team_names.keys())
    bf_team = st.selectbox(
        "Team(s)",
        bf_team_options,
        format_func=lambda x: f"{x} — {team_names[x]}" if x in team_names else x,
        key="bf_team",
    )

if st.button("Run Backfill", type="primary"):
    if bf_start > bf_end:
        st.error("Start date must be before end date.")
    else:
        from collectors.youtube_collector import (
            scan_channel, download_captions, _parse_subtitle_file,
        )
        from config_loader import get_settings, get_youtube_keywords
        import re
        import time

        settings = get_settings()
        keywords = get_youtube_keywords()

        # Calculate lookback hours from date range
        delta = datetime.now(tz=None) - datetime.combine(bf_start, datetime.min.time())
        lookback_hours = int(delta.total_seconds() / 3600) + 24

        # Override settings for backfill
        bf_settings = dict(settings)
        bf_settings["collection"] = dict(settings["collection"])
        bf_settings["collection"]["lookback_hours"] = lookback_hours
        bf_settings["collection"]["youtube_max_per_channel"] = 30

        date_str = f"backfill-{bf_start}-to-{bf_end}"
        bf_transcripts_dir = get_data_dir("transcripts", date_str)
        bf_temp_dir = get_data_dir("raw", date_str) / "youtube_temp"
        bf_temp_dir.mkdir(exist_ok=True)

        # Filter teams
        if bf_team == "All Teams":
            scan_teams = list(teams)
        else:
            scan_teams = [t for t in teams if t["abbr"] == bf_team]

        delay = settings["collection"].get("request_delay", 2.0)
        bf_end_dt = datetime.combine(bf_end, datetime.max.time())

        progress = st.progress(0, text="Starting backfill...")
        all_videos = []
        all_transcripts = []

        for idx, team in enumerate(scan_teams):
            abbr = team["abbr"]
            progress.progress(
                (idx + 1) / len(scan_teams),
                text=f"Scanning {abbr} ({team['name']})...",
            )

            videos = scan_channel(team, keywords, bf_settings, date_str)

            # Filter to date range
            for v in videos:
                upload = v.get("upload_date", "")
                if upload:
                    try:
                        v_date = datetime.strptime(upload, "%Y%m%d").date()
                        if v_date < bf_start or v_date > bf_end:
                            continue
                    except ValueError:
                        pass

                all_videos.append({**v, "team": abbr, "team_name": team["name"]})

                caption_path = download_captions(
                    v["video_id"], v["url"], bf_temp_dir, bf_settings,
                )
                transcript_text = None
                if caption_path:
                    transcript_text = _parse_subtitle_file(caption_path)

                if transcript_text and len(transcript_text.strip()) > 50:
                    slug = re.sub(r"[^\w\s-]", "", v["title"])[:60].strip().replace(" ", "_")
                    txt_path = bf_transcripts_dir / f"{abbr}_{slug}.txt"
                    txt_path.write_text(transcript_text, encoding="utf-8")

                    all_transcripts.append({
                        "video_id": v["video_id"],
                        "title": v["title"],
                        "team": abbr,
                        "team_name": team["name"],
                        "url": v["url"],
                        "upload_date": v.get("upload_date", ""),
                        "method": "captions",
                    })

                time.sleep(delay)
            time.sleep(delay)

        # Save metadata
        out_dir = get_data_dir("raw", date_str)
        with open(out_dir / "all_videos.json", "w", encoding="utf-8") as f:
            json.dump(all_videos, f, indent=2, ensure_ascii=False)
        with open(out_dir / "all_transcripts.json", "w", encoding="utf-8") as f:
            json.dump(all_transcripts, f, indent=2, ensure_ascii=False)

        progress.progress(1.0, text="Backfill complete!")
        st.success(
            f"Found {len(all_videos)} videos, downloaded {len(all_transcripts)} transcripts. "
            f"Saved to `data/transcripts/{date_str}/`"
        )

        # Show results
        if all_transcripts:
            st.markdown("**Backfilled videos:**")
            for t in all_transcripts:
                st.markdown(f"- **{t['team']}** — [{t['title']}]({t['url']})")

            st.markdown("**All URLs (copy for NotebookLM):**")
            st.code("\n".join(t["url"] for t in all_transcripts), language=None)

# ══════════════════════════════════════════════════════════
# Daily Browse — single date view with transcripts
# ══════════════════════════════════════════════════════════
st.divider()
st.subheader("Browse by Date")

selected_date = st.selectbox("Select date", all_transcript_dirs)
transcript_dir = transcripts_base / selected_date

youtube_meta = _load_youtube_meta(selected_date)
txt_files = _get_transcript_files(selected_date)

if not txt_files and not youtube_meta:
    st.info(f"No videos collected for {selected_date}.")
    st.stop()

# Team filter for browse
browse_teams = set()
for item in youtube_meta.values():
    browse_teams.add(item.get("team", ""))
for f in txt_files:
    abbr = f.name.split("_")[0]
    if abbr in team_names:
        browse_teams.add(abbr)

team_filter = st.multiselect(
    "Filter by team (leave empty for all)",
    sorted(browse_teams),
    format_func=lambda x: f"{x} — {team_names.get(x, x)}",
    key="browse_team_filter",
)

# YouTube links
if youtube_meta:
    st.markdown("**YouTube Links:**")
    for vid_id, meta in youtube_meta.items():
        team = meta.get("team", "")
        if team_filter and team not in team_filter:
            continue
        team_label = team_names.get(team, team)
        title = meta.get("title", "Unknown")
        url = meta.get("url", "")
        method = meta.get("method", "")
        channel = meta.get("channel_name", "")

        st.markdown(
            f"**{team_label}** — {title}  \n"
            f"[{url}]({url})  \n"
            f"*Source: {channel} YouTube channel • Transcribed via {method}*"
        )

    with st.expander("All links (copyable)"):
        links = []
        for meta in youtube_meta.values():
            team = meta.get("team", "")
            if team_filter and team not in team_filter:
                continue
            links.append(meta.get("url", ""))
        st.code("\n".join(links), language=None)

# Transcripts
if txt_files:
    st.markdown(f"**{len(txt_files)} transcripts available**")

    for txt_file in txt_files:
        abbr = txt_file.name.split("_")[0]
        if team_filter and abbr not in team_filter:
            continue

        team_name_display = team_names.get(abbr, abbr)
        display_name = txt_file.stem.replace("_", " ")

        video_url = ""
        for meta in youtube_meta.values():
            if meta.get("team", "") == abbr and display_name[:20] in meta.get("title", "").replace(" ", "_")[:20]:
                video_url = meta.get("url", "")
                break

        with st.expander(f"{team_name_display}: {display_name}"):
            if video_url:
                st.markdown(f"[Watch on YouTube]({video_url})")

            content = txt_file.read_text(encoding="utf-8")
            preview_len = 500
            if len(content) > preview_len:
                st.markdown(content[:preview_len] + "...")
            else:
                st.markdown(content)

            st.caption(f"Length: {len(content):,} characters")

            st.download_button(
                label=f"Download {txt_file.name}",
                data=content,
                file_name=txt_file.name,
                mime="text/plain",
                key=f"dl_{selected_date}_{txt_file.name}",
            )

# Bulk download
st.divider()
if txt_files and st.button("Download All as ZIP"):
    import io
    import zipfile

    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for txt_file in txt_files:
            abbr = txt_file.name.split("_")[0]
            if team_filter and abbr not in team_filter:
                continue
            zf.writestr(txt_file.name, txt_file.read_text(encoding="utf-8"))

    st.download_button(
        label="Download ZIP",
        data=zip_buffer.getvalue(),
        file_name=f"transcripts_{selected_date}.zip",
        mime="application/zip",
        key="zip_download",
    )

# Channel reference
with st.expander("YouTube channels being monitored"):
    st.markdown("Videos are collected from **all 32 official NFL team YouTube channels**:")
    cols = st.columns(4)
    for i, team in enumerate(sorted(teams, key=lambda t: t["name"])):
        channels = team.get("youtube_channels", [])
        cid = channels[0]["id"] if channels else team.get("youtube_channel_id", "")
        cols[i % 4].markdown(
            f"[{team['abbr']}](https://www.youtube.com/channel/{cid}) "
            f"— {team['name']}"
        )
