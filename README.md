# Margin Analytics

Interactive, password-gated Streamlit dashboard for Amazon margin
monitoring, fed by a weekly Novadata export committed to this repo.

## Pieces

| Path | Role |
| --- | --- |
| `novadata_weekly_export.py` | Downloads the Novadata margin CSV, cleans it, writes dated CSV + XLSX to `novadata_exports/`. |
| `.github/workflows/weekly_export.yml` | Runs the script every Monday 06:00 UTC (and on demand). Commits the new export back to the repo and uploads it as a workflow artifact. |
| `novadata_exports/margin_export_*.csv` | Committed snapshots — the dashboard always reads the most recent one. |
| `app/streamlit_app.py` | Password-gated dashboard with marketplace / period / SKU / top-seller filters, KPI cards, and a table with red/green CM3% and Days-of-Supply highlights. |
| `render.yaml` | Render Blueprint config. Render auto-redeploys on every push to `main`, so a fresh data commit ships a fresh dashboard. |

## Local dev

```bash
pip install -r requirements.txt

# Populate novadata_exports/ once
python novadata_weekly_export.py --once

# Run the dashboard
export DASHBOARD_PASSWORD="choose-a-password"
streamlit run app/streamlit_app.py
# → http://localhost:8501
```

## Deploy on Render

1. Push this repo to GitHub.
2. Render → **New → Blueprint** → point at the repo. `render.yaml` provisions
   the service.
3. Open the service → **Environment** → set `DASHBOARD_PASSWORD`. Save.
4. Render serves at `https://margin-analytics-XXXX.onrender.com`.

> Free Render plan sleeps after 15 min idle; first request after sleep takes
> ~30 s to wake.

## Weekly refresh

Once `weekly_export.yml` is on the default branch, GitHub Actions runs it
automatically. To trigger manually: **Actions → Novadata Weekly Export → Run
workflow**. Each successful run commits a new `margin_export_YYYY-MM-DD.csv`,
which Render picks up on the next deploy.

## Reference files

`Margin_Analytics.xlsx` + `build_workbook.py` are the earlier Google Sheets
template; kept in the repo for reference, not used by the dashboard.
