# Margin Analytics

Interactive, password-gated Streamlit dashboard for Amazon margin
monitoring, fed by a weekly Novadata export committed to this repo.

## Pieces

| Path | Role |
| --- | --- |
| `novadata_weekly_export.py` | Downloads the Novadata margin CSV, cleans it, writes dated CSV + XLSX to `novadata_exports/`. |
| `.github/workflows/weekly_export.yml` | Runs the script every Monday 06:00 UTC (and on demand). Commits the new export back to the repo and uploads it as a workflow artifact. |
| `novadata_exports/margin_export_*.csv` | Committed snapshots — the dashboard always reads the most recent one. |
| `streamlit_app.py` | Password-gated dashboard with Overview / Trend / Compare tabs. |
| `requirements.txt`, `runtime.txt`, `.streamlit/config.toml` | Streamlit Community Cloud deploy. |
| `render.yaml` | Alternative deploy on Render. |

## Deploy on Streamlit Community Cloud (recommended, free)

1. Go to **<https://share.streamlit.io>** and sign in with GitHub.
2. Click **Create app → Deploy a public app from GitHub**.
3. Fill in:
   - **Repository:** `ennoenno98/margin-analytics`
   - **Branch:** `claude/create-margin-analytics-ndZIK` (or `main` once merged)
   - **Main file path:** `streamlit_app.py`
4. Open **Advanced settings → Secrets** and paste:
   ```toml
   DASHBOARD_PASSWORD = "pick-something-strong"
   ```
5. Click **Deploy**. First build takes ~2 minutes.

The app URL will look like `https://margin-analytics-<hash>.streamlit.app`.
On each push to the deployed branch, Streamlit auto-redeploys.

## Local dev

```bash
pip install -r requirements.txt

# Populate novadata_exports/ once
python novadata_weekly_export.py --once

# Run the dashboard
export DASHBOARD_PASSWORD="choose-a-password"
streamlit run streamlit_app.py
# → http://localhost:8501
```

## Weekly refresh

Once `.github/workflows/weekly_export.yml` is on the deployed branch, GitHub
Actions runs it automatically. To trigger manually: **Actions → Novadata
Weekly Export → Run workflow**. Each successful run commits a new
`margin_export_YYYY-MM-DD.csv`, which Streamlit Community Cloud picks up on
the next deploy.

## Alternative: Render

`render.yaml` is included if you'd rather host on Render:
**New → Blueprint → pick the repo → set `DASHBOARD_PASSWORD` env var → Deploy.**

## Reference files

`Margin_Analytics.xlsx` + `build_workbook.py` are the earlier Google Sheets
template; kept in the repo for reference, not used by the dashboard.
