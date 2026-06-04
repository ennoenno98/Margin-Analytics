# Margin Analytics

Interactive, password-gated Streamlit dashboard for Amazon margin
monitoring, fed by a weekly Novadata export committed to this repo.

> **Sibling app:** [OOS Impact Analytics](README_OOS.md) (`oos_analytics.py`)
> tracks out-of-stock impact over the year — estimated lost revenue & CM3 per
> SKU — by combining the Amazon FBA Inventory Ledger with this margin export.
> It deploys as its own Streamlit app from the same repo.

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

   # Optional but recommended — persist Comments column edits across
   # container reboots. See "Persisting comments" below for setup.
   # GITHUB_TOKEN     = "ghp_..."
   # COMMENTS_GIST_ID = "abc123..."
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

## Persisting comments

Edits in the **Comments** column are written to `comments.json` on disk
*and*, when configured, mirrored to a private GitHub Gist so they
survive Streamlit Cloud's container reboots / redeploys.

One-time setup:

1. Create a private Gist with a single file named exactly
   `comments.json` containing `{}` — copy the **Gist ID** (the long
   hash in the URL).
2. Create a **fine-grained personal access token** (GitHub → Settings
   → Developer settings → Personal access tokens → Fine-grained tokens).
   Scope: **Gists → Read and write**. No repository permissions needed.
3. In Streamlit Cloud → **Manage app → Settings → Secrets**, add:
   ```toml
   GITHUB_TOKEN     = "github_pat_..."
   COMMENTS_GIST_ID = "abc123..."
   ```
4. Reboot the app. The Comments column will now load from and write to
   the Gist, with a small "synced to Gist ✓" indicator under the table.

Without those secrets the app falls back to session-only comments and
shows a hint near the table.

## Alternative: Render

`render.yaml` is included if you'd rather host on Render:
**New → Blueprint → pick the repo → set `DASHBOARD_PASSWORD` env var → Deploy.**

## Reference files

`Margin_Analytics.xlsx` + `build_workbook.py` are the earlier Google Sheets
template; kept in the repo for reference, not used by the dashboard.
