"""Fetch the FBA Inventory Ledger (Detailed View) via the Amazon Selling
Partner API — the automated replacement for the manual Seller Central download.

Instead of *Seller Central → Reports → Fulfilment → Inventory Ledger →
"Detailed View" → download*, this requests the same report
(``GET_LEDGER_DETAIL_VIEW_DATA``) through the SP-API Reports API, downloads the
result, and hands it to ``add_ledger.main`` — so it lands as a dated,
gzipped ``amazon_ledger/inventory_ledger_*.csv.gz`` exactly like the manual
flow, and ``oos_analytics.py`` picks it up unchanged.

Auth is Login-with-Amazon only (no AWS IAM / request signing since 2023). You
create a *self-authorized* SP-API app on your own seller account and provide
three secrets via environment variables:

    LWA_CLIENT_ID        # from your SP-API app
    LWA_CLIENT_SECRET    # from your SP-API app
    SP_API_REFRESH_TOKEN # from self-authorizing the app to your account

Optional overrides (sensible EU defaults otherwise):

    SP_API_ENDPOINT           # default https://sellingpartnerapi-eu.amazon.com
    SP_API_MARKETPLACE_IDS    # comma-separated; default = EU + UK set below
    LEDGER_LOOKBACK_DAYS      # default 365

Run:
    pip install -r requirements-export.txt
    python amazon_sp_api_ledger.py            # then commit amazon_ledger/
"""
from __future__ import annotations

import gzip
import io
import logging
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests

import add_ledger

# ─── Configuration ────────────────────────────────────────────────────
LWA_TOKEN_URL = "https://api.amazon.com/auth/o2/token"
DEFAULT_ENDPOINT = "https://sellingpartnerapi-eu.amazon.com"  # Europe region
REPORT_TYPE = "GET_LEDGER_DETAIL_VIEW_DATA"

# Europe-region marketplaces (one report call may span several). GB is kept in
# the request; oos_analytics.py handles the Pan-EU pool vs. GB split downstream.
DEFAULT_MARKETPLACE_IDS = [
    "A1PA6795UKMFR9",  # DE
    "A13V1IB3VIYZZH",  # FR
    "APJ6JRA9NG5V4",   # IT
    "A1RKKUPIHCS9HS",  # ES
    "A1805IZSGTT6HS",  # NL
    "A2NODRKZP88ZB9",  # SE
    "A1C3SOZRARQ6R3",  # PL
    "AMEN7PMS3EDWL",   # BE
    "A1F83G8C2ARO7P",  # UK / GB
]

POLL_INTERVAL_S = 20
POLL_TIMEOUT_S = 15 * 60  # reports usually finish in 1-5 minutes

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)


# ─── Auth ─────────────────────────────────────────────────────────────
def _require_env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        sys.exit(
            f"Missing required environment variable: {name}. Set LWA_CLIENT_ID, "
            "LWA_CLIENT_SECRET and SP_API_REFRESH_TOKEN (see the module docstring)."
        )
    return val


def get_access_token() -> str:
    """Exchange the long-lived LWA refresh token for a 1-hour access token."""
    resp = requests.post(
        LWA_TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "refresh_token": _require_env("SP_API_REFRESH_TOKEN"),
            "client_id": _require_env("LWA_CLIENT_ID"),
            "client_secret": _require_env("LWA_CLIENT_SECRET"),
        },
        timeout=60,
    )
    if resp.status_code != 200:
        sys.exit(f"LWA token request failed ({resp.status_code}): {resp.text}")
    return resp.json()["access_token"]


# ─── Reports API flow ─────────────────────────────────────────────────
def _headers(token: str) -> dict:
    return {"x-amz-access-token": token, "Content-Type": "application/json"}


def create_report(endpoint: str, token: str, marketplace_ids: list[str],
                  lookback_days: int) -> str:
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=lookback_days)
    body = {
        "reportType": REPORT_TYPE,
        "marketplaceIds": marketplace_ids,
        "dataStartTime": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "dataEndTime": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    log.info("Requesting %s for %d marketplace(s), %s → %s",
             REPORT_TYPE, len(marketplace_ids),
             start.date(), end.date())
    resp = requests.post(f"{endpoint}/reports/2021-06-30/reports",
                         headers=_headers(token), json=body, timeout=60)
    if resp.status_code not in (200, 201, 202):
        sys.exit(f"createReport failed ({resp.status_code}): {resp.text}")
    return resp.json()["reportId"]


def poll_report(endpoint: str, token: str, report_id: str) -> str:
    """Poll until the report is DONE; return its reportDocumentId."""
    deadline = time.monotonic() + POLL_TIMEOUT_S
    while True:
        resp = requests.get(f"{endpoint}/reports/2021-06-30/reports/{report_id}",
                            headers=_headers(token), timeout=60)
        if resp.status_code != 200:
            sys.exit(f"getReport failed ({resp.status_code}): {resp.text}")
        info = resp.json()
        status = info.get("processingStatus")
        log.info("Report %s: %s", report_id, status)
        if status == "DONE":
            return info["reportDocumentId"]
        if status in ("CANCELLED", "FATAL"):
            sys.exit(f"Report ended with status {status}. Full response: {info}")
        if time.monotonic() > deadline:
            sys.exit(f"Report {report_id} not done after {POLL_TIMEOUT_S}s "
                     f"(last status: {status}).")
        time.sleep(POLL_INTERVAL_S)


def download_document(endpoint: str, token: str, document_id: str) -> str:
    """Fetch the report document (a presigned URL) and return decoded text."""
    resp = requests.get(
        f"{endpoint}/reports/2021-06-30/documents/{document_id}",
        headers=_headers(token), timeout=60,
    )
    if resp.status_code != 200:
        sys.exit(f"getReportDocument failed ({resp.status_code}): {resp.text}")
    doc = resp.json()
    # The presigned S3 URL takes no auth header.
    raw = requests.get(doc["url"], timeout=300).content
    if doc.get("compressionAlgorithm") == "GZIP":
        raw = gzip.decompress(raw)
    # FBA reports are usually UTF-8 but occasionally Latin-1 / Cp1252.
    for enc in ("utf-8", "cp1252", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


# ─── Normalisation → hand off to add_ledger ───────────────────────────
def to_ledger_csv(report_text: str, tmp_dir: Path) -> Path:
    """SP-API report documents are tab-delimited. Convert to the comma CSV that
    add_ledger.py / the dashboard expect, normalising Date to MM/DD/YYYY."""
    df = pd.read_csv(io.StringIO(report_text), sep="\t", dtype=str)
    if "Date" not in df.columns or "MSKU" not in df.columns:
        sys.exit("Downloaded report is not an Inventory Ledger Detailed View "
                 f"(columns: {list(df.columns)[:12]}…).")
    # Re-emit Date as MM/DD/YYYY so add_ledger's parser accepts it regardless
    # of the marketplace locale the report came back in. Real reports use one
    # uniform format; try the expected US format first, then infer.
    parsed = pd.to_datetime(df["Date"], format="%m/%d/%Y", errors="coerce")
    if parsed.isna().mean() > 0.02:
        parsed = pd.to_datetime(df["Date"], errors="coerce")
    if parsed.notna().mean() >= 0.98:
        df["Date"] = parsed.dt.strftime("%m/%d/%Y")
    else:
        log.warning("Could not confidently normalise the Date column; passing "
                    "it through unchanged for add_ledger to validate.")
    out = tmp_dir / "inventory-ledger.csv"
    df.to_csv(out, index=False)
    log.info("Report parsed: %d rows, %d columns.", len(df), df.shape[1])
    return out


def main() -> None:
    endpoint = os.environ.get("SP_API_ENDPOINT", DEFAULT_ENDPOINT).rstrip("/")
    ids_env = os.environ.get("SP_API_MARKETPLACE_IDS")
    marketplace_ids = (
        [m.strip() for m in ids_env.split(",") if m.strip()]
        if ids_env else DEFAULT_MARKETPLACE_IDS
    )
    lookback = int(os.environ.get("LEDGER_LOOKBACK_DAYS", "365"))

    token = get_access_token()
    report_id = create_report(endpoint, token, marketplace_ids, lookback)
    document_id = poll_report(endpoint, token, report_id)
    text = download_document(endpoint, token, document_id)

    with tempfile.TemporaryDirectory() as td:
        csv_path = to_ledger_csv(text, Path(td))
        # Reuse the existing helper: gzip + date-stamp into amazon_ledger/,
        # drop superseded synthetic tails, prune old snapshots.
        add_ledger.main(str(csv_path))


if __name__ == "__main__":
    main()
