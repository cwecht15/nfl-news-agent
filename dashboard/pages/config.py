"""Configuration Editor — Edit sources.yaml and settings.yaml from the browser.

Writes a timestamped backup to config/backups/ before overwriting, validates
YAML syntax on save, and clears the config_loader caches so new values take
effect in the current session without a dashboard restart.
"""

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import streamlit as st

st.set_page_config(page_title="Config", page_icon="🏈", layout="wide")

from dashboard.auth import require_password
require_password()

from dashboard.helpers import stop_if_not_local
stop_if_not_local("Config")

import yaml

import config_loader

CONFIG_DIR = Path(config_loader.CONFIG_DIR)
BACKUP_DIR = CONFIG_DIR / "backups"
BACKUP_KEEP = 20

EDITABLE_FILES = {
    "sources.yaml": {
        "label": "Sources (sources.yaml)",
        "help": (
            "RSS feeds, beat writers, web sources, and YouTube keywords. "
            "Add beat writers under `beat_writers:` — see the comments in the "
            "file for the schema."
        ),
        "cache_clear": [config_loader.get_sources],
    },
    "settings.yaml": {
        "label": "Settings (settings.yaml)",
        "help": (
            "Collection windows, summarization provider, Whisper model, "
            "cross-day dedup threshold, storage retention."
        ),
        "cache_clear": [config_loader.get_settings],
    },
    "teams.yaml": {
        "label": "Teams (teams.yaml)",
        "help": (
            "All 32 team records including YouTube channel IDs. Usually you "
            "don't need to edit this."
        ),
        "cache_clear": [config_loader.get_teams, config_loader.get_teams_by_abbr],
    },
}


def _list_backups(filename: str) -> list[Path]:
    if not BACKUP_DIR.exists():
        return []
    stem = filename.rsplit(".", 1)[0]
    return sorted(
        BACKUP_DIR.glob(f"{stem}.*.yaml"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )


def _prune_backups(filename: str):
    backups = _list_backups(filename)
    for old in backups[BACKUP_KEEP:]:
        try:
            old.unlink()
        except OSError:
            pass


def _write_backup(filename: str, content: str) -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stem = filename.rsplit(".", 1)[0]
    ts = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    backup_path = BACKUP_DIR / f"{stem}.{ts}.yaml"
    backup_path.write_text(content, encoding="utf-8")
    _prune_backups(filename)
    return backup_path


def _clear_caches(cache_clear_targets):
    for target in cache_clear_targets:
        if target is None:
            continue
        cache = getattr(target, "cache_clear", None)
        if callable(cache):
            cache()


def _render_file_editor(filename: str, meta: dict):
    path = CONFIG_DIR / filename
    if not path.exists():
        st.error(f"File not found: {path}")
        return

    st.caption(meta["help"])

    # Key the textarea per file so switching tabs preserves unsaved edits
    session_key = f"config_edit_{filename}"
    disk_content = path.read_text(encoding="utf-8")

    # Initialize or reset from disk on first render or after revert
    if session_key not in st.session_state:
        st.session_state[session_key] = disk_content

    # Show drift warning if on-disk content differs from session buffer
    if st.session_state[session_key] != disk_content and not st.session_state.get(
        f"{session_key}_dirty_confirmed", False
    ):
        st.info(
            "You have unsaved edits. The on-disk file may have changed since "
            "you opened this editor — review before saving."
        )

    st.text_area(
        f"Content of {filename}",
        key=session_key,
        height=600,
        label_visibility="collapsed",
    )

    col_save, col_revert, col_info = st.columns([1, 1, 3])

    with col_save:
        save_clicked = st.button(
            "Validate & Save",
            key=f"save_{filename}",
            type="primary",
            use_container_width=True,
        )
    with col_revert:
        revert_clicked = st.button(
            "Revert to disk",
            key=f"revert_{filename}",
            use_container_width=True,
        )

    backups = _list_backups(filename)
    with col_info:
        if backups:
            st.caption(
                f"Last backup: {backups[0].name}  ·  "
                f"{len(backups)} backup(s) on disk (keeping latest {BACKUP_KEEP})"
            )
        else:
            st.caption("No backups yet — one will be created on your next save.")

    if revert_clicked:
        st.session_state[session_key] = path.read_text(encoding="utf-8")
        st.session_state[f"{session_key}_dirty_confirmed"] = False
        st.success("Reverted to on-disk content.")
        st.rerun()

    if save_clicked:
        new_content = st.session_state[session_key]

        # Validate YAML
        try:
            parsed = yaml.safe_load(new_content)
        except yaml.YAMLError as e:
            st.error(f"**YAML syntax error.** Nothing was saved.\n\n```\n{e}\n```")
            return

        if parsed is None:
            st.error("The file would be empty after parsing. Save aborted.")
            return

        # Write backup of CURRENT on-disk content before overwriting
        try:
            backup_path = _write_backup(filename, path.read_text(encoding="utf-8"))
        except OSError as e:
            st.error(f"Failed to write backup — save aborted: {e}")
            return

        # Write new content
        try:
            path.write_text(new_content, encoding="utf-8")
        except OSError as e:
            st.error(f"Failed to write {filename}: {e}")
            return

        # Invalidate config_loader caches so new values apply in this session
        _clear_caches(meta["cache_clear"])

        st.session_state[f"{session_key}_dirty_confirmed"] = False
        st.success(
            f"Saved {filename}.  Backup: `{backup_path.name}`.  "
            "Config caches were cleared."
        )


def _render_backups_panel():
    st.subheader("Restore a backup")
    st.caption(
        "If a save broke something, pick a backup below to restore it. "
        "A new backup of the current file is taken before the restore."
    )

    if not BACKUP_DIR.exists():
        st.info("No backups yet.")
        return

    all_backups = sorted(
        BACKUP_DIR.glob("*.yaml"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not all_backups:
        st.info("No backups yet.")
        return

    options = [b.name for b in all_backups]
    selected = st.selectbox("Backup file", options)
    backup_path = BACKUP_DIR / selected

    # Guess target filename: "<stem>.<timestamp>.yaml" -> "<stem>.yaml"
    parts = selected.split(".")
    if len(parts) >= 3:
        target_name = f"{parts[0]}.yaml"
    else:
        target_name = None

    col_view, col_restore = st.columns([1, 1])
    with col_view:
        with st.expander("Preview backup contents", expanded=False):
            try:
                st.code(backup_path.read_text(encoding="utf-8"), language="yaml")
            except OSError as e:
                st.error(f"Failed to read backup: {e}")
    with col_restore:
        if target_name and target_name in EDITABLE_FILES:
            if st.button(
                f"Restore to {target_name}",
                type="primary",
                use_container_width=True,
            ):
                target_path = CONFIG_DIR / target_name
                try:
                    current = target_path.read_text(encoding="utf-8")
                    _write_backup(target_name, current)
                    target_path.write_text(
                        backup_path.read_text(encoding="utf-8"),
                        encoding="utf-8",
                    )
                    _clear_caches(EDITABLE_FILES[target_name]["cache_clear"])
                    # Also refresh the editor's session buffer
                    session_key = f"config_edit_{target_name}"
                    if session_key in st.session_state:
                        del st.session_state[session_key]
                    st.success(f"Restored {target_name} from {selected}.")
                    st.rerun()
                except OSError as e:
                    st.error(f"Restore failed: {e}")
        else:
            st.caption("Cannot determine target file name.")


# ─── Main page ───────────────────────────────────────────────────────

st.header("Configuration Editor")
st.markdown(
    "Edit the YAML config files directly. Each save writes a timestamped "
    "backup to `config/backups/` first, and YAML is validated before "
    "overwriting. Invalid YAML is rejected with the parse error."
)

tabs = st.tabs(
    [meta["label"] for meta in EDITABLE_FILES.values()] + ["Backups"]
)

for (filename, meta), tab in zip(EDITABLE_FILES.items(), tabs[:-1]):
    with tab:
        _render_file_editor(filename, meta)

with tabs[-1]:
    _render_backups_panel()
