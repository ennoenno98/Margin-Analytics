"""
Novadata Weekly Export Script
==============================
Lädt wöchentlich die Margin-Datei von Novadata herunter und speichert sie
als CSV + Excel (optional). Kann als Cronjob oder manuell ausgeführt werden.

Setup:
    pip install requests pandas openpyxl schedule

Ausführung:
    - Einmalig:   python novadata_weekly_export.py --once
    - Wöchentlich (automatisch): python novadata_weekly_export.py
"""

import requests
import pandas as pd
import os
import sys
import logging
import schedule
import time
from datetime import datetime
from pathlib import Path

# ─────────────────────────────────────────────
# KONFIGURATION – hier anpassen
# ─────────────────────────────────────────────

DOWNLOAD_URL = "https://app.novadata.io/resources/data-export-download/17715a7b-7eb5-4a11-9476-970df01c7bca"

# Zielordner für die exportierten Dateien
OUTPUT_DIR = Path("./novadata_exports")

# Ausgabeformate: "csv", "excel" oder beides ["csv", "excel"]
OUTPUT_FORMATS = ["csv", "excel"]

# Wochentag und Uhrzeit für den automatischen Download (z.B. "monday" um "06:00")
SCHEDULE_DAY = "monday"
SCHEDULE_TIME = "06:00"

# Optional: Ob alte Dateien (> X Wochen) automatisch gelöscht werden sollen
DELETE_OLD_FILES_AFTER_WEEKS = 12  # None = nie löschen

# ─────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("novadata_export.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# HAUPTFUNKTIONEN
# ─────────────────────────────────────────────

def download_file(url: str, dest_path: Path) -> bool:
    """Lädt eine Datei von der URL herunter."""
    log.info(f"Lade Datei herunter: {url}")
    try:
        response = requests.get(url, timeout=120, stream=True)
        response.raise_for_status()

        with open(dest_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)

        size_mb = dest_path.stat().st_size / 1024 / 1024
        log.info(f"Download erfolgreich: {dest_path.name} ({size_mb:.1f} MB)")
        return True

    except requests.exceptions.RequestException as e:
        log.error(f"Download fehlgeschlagen: {e}")
        return False


def process_and_export(raw_csv_path: Path, date_str: str) -> None:
    """Liest die CSV, bereinigt sie und exportiert in die gewünschten Formate."""
    log.info("Lese und verarbeite CSV...")

    df = pd.read_csv(raw_csv_path, sep=None, engine="python")
    log.info(f"Datei geladen: {len(df)} Zeilen, {len(df.columns)} Spalten")

    # ── Bereinigung ──────────────────────────────────────────
    # Spaltentypen korrigieren
    numeric_cols = ["CM1%", "CM2%", "CM3%", "Sponsored Spend", "ROAS",
                    "CTR", "Orders", "Units", "Product Sales",
                    "FBA Available", "Days of Supply", "Sales Velocity"]
    for col in numeric_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    # Datum normalisieren
    if "Period" in df.columns:
        df["Period"] = pd.to_datetime(df["Period"], errors="coerce")

    log.info("Bereinigung abgeschlossen.")

    # ── Export ───────────────────────────────────────────────
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    if "csv" in OUTPUT_FORMATS:
        csv_out = OUTPUT_DIR / f"margin_export_{date_str}.csv"
        df.to_csv(csv_out, index=False, encoding="utf-8-sig")
        log.info(f"CSV gespeichert: {csv_out}")

    if "excel" in OUTPUT_FORMATS:
        excel_out = OUTPUT_DIR / f"margin_export_{date_str}.xlsx"
        with pd.ExcelWriter(excel_out, engine="openpyxl") as writer:
            df.to_excel(writer, index=False, sheet_name="Margin Data")

            # Spaltenbreite automatisch anpassen
            ws = writer.sheets["Margin Data"]
            for col_cells in ws.columns:
                max_len = max(
                    (len(str(c.value)) for c in col_cells if c.value),
                    default=10
                )
                ws.column_dimensions[col_cells[0].column_letter].width = min(max_len + 2, 50)

        log.info(f"Excel gespeichert: {excel_out}")


def cleanup_old_files() -> None:
    """Löscht alte Exportdateien, die älter als X Wochen sind."""
    if DELETE_OLD_FILES_AFTER_WEEKS is None:
        return

    cutoff = pd.Timestamp.now() - pd.Timedelta(weeks=DELETE_OLD_FILES_AFTER_WEEKS)
    for f in OUTPUT_DIR.glob("margin_export_*.csv"):
        if pd.Timestamp(f.stat().st_mtime, unit="s") < cutoff:
            f.unlink()
            log.info(f"Alte Datei gelöscht: {f.name}")
    for f in OUTPUT_DIR.glob("margin_export_*.xlsx"):
        if pd.Timestamp(f.stat().st_mtime, unit="s") < cutoff:
            f.unlink()
            log.info(f"Alte Datei gelöscht: {f.name}")


def run_export() -> None:
    """Hauptprozess: Download → Verarbeitung → Export → Cleanup."""
    date_str = datetime.now().strftime("%Y-%m-%d")
    log.info(f"=== Starte wöchentlichen Export ({date_str}) ===")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    raw_path = OUTPUT_DIR / f"_raw_{date_str}.csv"

    success = download_file(DOWNLOAD_URL, raw_path)
    if not success:
        log.error("Export abgebrochen wegen Download-Fehler.")
        return

    process_and_export(raw_path, date_str)

    # Rohdatei löschen (bereits verarbeitet)
    raw_path.unlink(missing_ok=True)

    cleanup_old_files()
    log.info("=== Export abgeschlossen ===")


# ─────────────────────────────────────────────
# EINSTIEGSPUNKT
# ─────────────────────────────────────────────

if __name__ == "__main__":
    if "--once" in sys.argv:
        # Einmaliger Sofort-Download
        run_export()
    else:
        # Wöchentlicher Zeitplan
        log.info(f"Scheduler gestartet: läuft jeden {SCHEDULE_DAY} um {SCHEDULE_TIME} Uhr")
        getattr(schedule.every(), SCHEDULE_DAY).at(SCHEDULE_TIME).do(run_export)

        # Optional: Sofort einmal ausführen beim Start
        run_export()

        while True:
            schedule.run_pending()
            time.sleep(60)
