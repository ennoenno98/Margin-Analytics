"""Novadata Daily Export Script
==============================

Lädt die tägliche Margin-Datei von Novadata herunter (rolling 12 Monate)
und speichert sie als gzipped CSV. Kann als Cronjob oder manuell laufen.

Setup:
    pip install -r requirements-export.txt

Ausführung:
    python novadata_weekly_export.py --once
"""

import gzip
import logging
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests


# ─── Konfiguration ────────────────────────────────────────────────────
# Tägliches Margin-Export (12-Monats-Fenster, mit täglicher Granularität,
# inklusive absoluter Contribution Margin 1/2/3 und Advertising Costs).
DOWNLOAD_URL = (
    "https://app.novadata.io/resources/data-export-download/"
    "d664ab4b-047a-471b-a993-27dd42d0a91b"
)
OUTPUT_DIR = Path("./novadata_exports")
SCHEDULE_TIME = "06:00"        # für Standalone-Modus ohne --once
DELETE_OLD_FILES_AFTER_DAYS = 14  # Aufräumen alter Snapshots im Repo

# ─── Logging ──────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("novadata_export.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)


def download_file(url: str, dest_path: Path) -> bool:
    log.info(f"Lade Datei herunter: {url}")
    try:
        with requests.get(url, timeout=300, stream=True) as response:
            response.raise_for_status()
            with open(dest_path, "wb") as f:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    f.write(chunk)
        size_mb = dest_path.stat().st_size / 1024 / 1024
        log.info(f"Download erfolgreich: {dest_path.name} ({size_mb:.1f} MB)")
        return True
    except requests.exceptions.RequestException as e:
        log.error(f"Download fehlgeschlagen: {e}")
        return False


def gzip_in_place(src: Path, dest: Path) -> None:
    """Komprimiere src zu dest (gzip), lösche das Original."""
    log.info(f"Komprimiere → {dest.name}")
    with open(src, "rb") as fin, gzip.open(dest, "wb", compresslevel=6) as fout:
        shutil.copyfileobj(fin, fout, length=1024 * 1024)
    src.unlink(missing_ok=True)
    size_mb = dest.stat().st_size / 1024 / 1024
    log.info(f"Komprimiert: {dest.name} ({size_mb:.1f} MB)")


def cleanup_old_files() -> None:
    if DELETE_OLD_FILES_AFTER_DAYS is None:
        return
    cutoff = pd.Timestamp.now() - pd.Timedelta(days=DELETE_OLD_FILES_AFTER_DAYS)
    for pattern in ("margin_export_*.csv", "margin_export_*.csv.gz", "margin_export_*.xlsx"):
        for f in OUTPUT_DIR.glob(pattern):
            if pd.Timestamp(f.stat().st_mtime, unit="s") < cutoff:
                f.unlink()
                log.info(f"Alte Datei gelöscht: {f.name}")


def run_export() -> None:
    date_str = datetime.now().strftime("%Y-%m-%d")
    log.info(f"=== Starte täglichen Export ({date_str}) ===")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    raw_path = OUTPUT_DIR / f"_raw_{date_str}.csv"
    final_path = OUTPUT_DIR / f"margin_export_{date_str}.csv.gz"

    if not download_file(DOWNLOAD_URL, raw_path):
        log.error("Export abgebrochen.")
        return

    # Quick sanity check that the file is the expected CSV (Novadata sometimes
    # returns an HTML error page if the link expired).
    with open(raw_path, "rb") as f:
        head = f.read(512)
    if b"Period" not in head and b"," not in head:
        log.error(
            "Heruntergeladene Datei sieht nicht wie eine CSV aus "
            "(eventuell abgelaufener Link?)."
        )
        raw_path.unlink(missing_ok=True)
        return

    gzip_in_place(raw_path, final_path)
    cleanup_old_files()
    log.info("=== Export abgeschlossen ===")


if __name__ == "__main__":
    if "--once" in sys.argv:
        run_export()
    else:
        import schedule  # only needed for the standalone loop
        log.info(f"Scheduler gestartet: täglich {SCHEDULE_TIME} Uhr")
        schedule.every().day.at(SCHEDULE_TIME).do(run_export)
        run_export()
        while True:
            schedule.run_pending()
            time.sleep(60)
