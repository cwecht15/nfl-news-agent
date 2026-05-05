"""Snapshot the master '2026 Depth Chart' Google Sheet to a local JSON file.

Reads the DepthCharts and Transactions_New tabs and writes the raw rows
to data/master_sheet/latest.json. The Streamlit Depth Chart Manager
page reads this file instead of calling Sheets directly — so the live
site doesn't need Google service-account credentials in Streamlit
secrets, and a stale snapshot can be refreshed by re-running this
script (locally or via the GitHub Actions workflow_dispatch button).

Run locally:
    python scripts/snapshot_master_sheet.py

GitHub Actions: see .github/workflows/master_sheet_snapshot.yml
"""

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from processing.sheet_reconciliation import (
    SNAPSHOT_PATH,
    _get_gspread_client,
    fetch_depthchart_rows,
    fetch_transactions_rows,
)

logger = logging.getLogger(__name__)


def snapshot() -> Path:
    client = _get_gspread_client()
    logger.info("Fetching DepthCharts rows...")
    depthchart_rows = fetch_depthchart_rows(client)
    logger.info("Fetching Transactions_New rows...")
    transactions_rows = fetch_transactions_rows(client)

    payload = {
        "snapshot_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "depthchart_rows": depthchart_rows,
        "transactions_rows": transactions_rows,
        "depthchart_row_count": len(depthchart_rows),
        "transactions_row_count": len(transactions_rows),
    }

    SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(SNAPSHOT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    size_kb = SNAPSHOT_PATH.stat().st_size / 1024
    logger.info(
        "Wrote %s (%d depth-chart rows, %d transaction rows, %.1f KB)",
        SNAPSHOT_PATH, len(depthchart_rows), len(transactions_rows), size_kb,
    )
    return SNAPSHOT_PATH


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    snapshot()
