# SDR Ops Command Center

Local Python tool that runs three SDR workflows from one Streamlit UI.

- **📊 Weekly SDR KPI Report** — HubSpot + Apollo metrics per SDR, posted to Slack
- **🗺️ Territory Lookalike** — One customer/day → similar AEC firms → Apollo list
- **🎯 Daily Prospect List** — AEC universe − customers − recently engaged → Apollo list

## Quick start

```bash
# 1. Install
pip install -r requirements.txt

# 2. Config
cp .env.example .env
# Fill in HUBSPOT_API_KEY, APOLLO_API_KEY, SLACK_*, SDR_USERS_JSON

# 3. Export AEC firms from RDS to CSV (one-time)
# See "AEC firm snapshot" section below
# Drop the file at data/aec_firms.csv

# 4. Run the dashboard
streamlit run app.py
# Opens at http://localhost:8501
```

## What you need before running

### 1. SDR user IDs (`SDR_USERS_JSON` in `.env`)

For each SDR, you need their **HubSpot owner ID** and **Apollo user ID**.

**HubSpot owner ID:**
- Settings → Users & Teams → click the SDR → copy `ownerId` from the URL, OR
- API: `GET https://api.hubapi.com/crm/v3/owners` (returns all owners)

**Apollo user ID:**
- API: `POST https://api.apollo.io/api/v1/users/search` with empty body returns all users
- Each user has an `id` field

Format in `.env`:
```
SDR_USERS_JSON={"Walter":{"hubspot_owner_id":"12345678","apollo_user_id":"abc..."},"Nolan":{...},"Quinn":{...}}
```

### 2. Slack delivery — pick one

**Option A — Webhook (simpler):**
- Create incoming webhook at https://api.slack.com/messaging/webhooks
- Set `SLACK_WEBHOOK_URL` in `.env`
- Channel is whatever the webhook points to

**Option B — Bot token (more flexibility):**
- Create app at https://api.slack.com/apps with `chat:write` scope
- Install to workspace, copy Bot User OAuth Token
- Set `SLACK_BOT_TOKEN` and `SLACK_KPI_CHANNEL` / `SLACK_PROSPECTS_CHANNEL` in `.env`

### 3. AEC firm snapshot (`data/aec_firms.csv`)

Required columns:
- `name`
- `domain`
- `city`
- `state`
- `employee_count`
- `annual_revenue`
- `aec_tag` (A / E / C / mixed)

**Export from your RDS** (you already have SSH tunnel access):

```bash
# Local tunnel
ssh -L 5432:fds-int-100m-cluster:5432 bastion

# Then in another terminal
psql -h localhost -U <user> -d <db> -c "\copy (SELECT name, domain, city, state, employee_count, annual_revenue, aec_tag FROM aec_firms) TO 'data/aec_firms.csv' WITH CSV HEADER"
```

Adjust the table/column names to match your actual schema.

## Project structure

```
sdr-ops/
├── app.py                    Streamlit dashboard
├── workflows/
│   ├── kpi_report.py         Project 1
│   ├── territory_lookalike.py Project 2
│   └── daily_prospects.py    Project 3
├── lib/
│   ├── hubspot.py            HubSpot API helpers
│   ├── apollo.py             Apollo API helpers
│   ├── slack.py              Slack Block Kit posters
│   └── logger.py             JSON run logs to ./logs/
├── data/aec_firms.csv        (you create this)
├── logs/                     auto-populated
├── outputs/                  auto-populated CSVs
├── .env                      (you create this from .env.example)
└── requirements.txt
```

## Running workflows

### Via dashboard (recommended)
```bash
streamlit run app.py
```
Click "▶ Run now" on any card.

### Via CLI
```bash
python -m workflows.kpi_report
python -m workflows.territory_lookalike
python -m workflows.daily_prospects
```

### Via cron (optional, when ready to schedule)
```cron
0 13 * * 1  cd /path/to/sdr-ops && /path/to/venv/bin/python -m workflows.kpi_report
0 11 * * *  cd /path/to/sdr-ops && /path/to/venv/bin/python -m workflows.territory_lookalike
0 21 * * *  cd /path/to/sdr-ops && /path/to/venv/bin/python -m workflows.daily_prospects
```

### Slack KPI bot (on-demand queries)
```bash
python -m workflows.slack_kpi_bot
```

Example Slack queries:
```text
@SDR Ops Bot kpi for Walter last 7 days
@SDR Ops Bot kpi for Nolan this week
@SDR Ops Bot kpi for SDRs last month
@SDR Ops Bot kpi from 2026-06-01 to 2026-06-10 for Quinn
```

Required Slack env vars for the bot:
- `SLACK_BOT_TOKEN`
- `SLACK_APP_TOKEN`

Required Slack app settings:
- Socket Mode: on
- Bot scopes:
  - `app_mentions:read`
  - `chat:write`
  - `channels:history`
  - `im:history`
- Bot events:
  - `app_mention`
  - `message.im`

## Deployment

For always-on use, deploy:
- one **Background Worker** for the Slack bot
- one **Cron Job** for the weekly KPI post

This repo includes [render.yaml](./render.yaml) for Render.

### Render deployment plan

Worker:
- Name: `sdr-ops-slack-bot`
- Command: `python -m workflows.slack_kpi_bot`

Cron:
- Name: `sdr-ops-kpi-weekly`
- Schedule: `0 13 * * 1`
- Command: `python -m workflows.kpi_report`

### Required env vars in Render

- `HUBSPOT_API_KEY`
- `APOLLO_API_KEY`
- `SLACK_BOT_TOKEN`
- `SLACK_APP_TOKEN` for the worker
- `SLACK_KPI_CHANNEL`
- `SLACK_PROSPECTS_CHANNEL`
- `SDR_USERS_JSON`
- `APOLLO_CONNECTED_OUTCOME_IDS`
- `HUBSPOT_CONNECTED_DISPOSITION_IDS`

### What you need to do manually

1. Create a new GitHub repo for this project.
2. Push this folder to that repo.
3. In Render, create a Blueprint or manually create services from `render.yaml`.
4. Paste the real env var values into Render.
5. Let the worker deploy.
6. Test Slack mention queries.
7. Trigger the weekly cron manually once to validate the scheduled KPI post.

## Known gaps & fallbacks

- **Apollo cold-call disposition data** is API-tier-limited. The KPI report will show call counts where available and flag the gap in the Slack post. Escalate tier upgrade with Pedro if leadership needs full disposition data.
- **HubSpot email opens/clicks** require the email events API. The current `aggregate_email_metrics` only counts sends + replies; opens come from Apollo's sequence data, which is the more reliable source anyway.
- **Apollo list push** falls back to CSV export if the labels API isn't available on your tier. CSV is written to `outputs/` regardless.
- **Lookalike geo filter** is state-level only for now. Lat/lng radius requires geocoding; punt until v2.

## Next steps after demo

- Move JSON logs to SQLite for queryable history
- Add per-workflow detail pages in `pages/` for full log drill-down
- Wire `cron` (or migrate to GitHub Actions when ready to deploy)
- Tune lookalike scoring with SDR feedback after first week

## Tomorrow's SDR meeting demo flow

1. Open dashboard at `localhost:8501` — three workflow cards visible
2. Click "▶ Run now" on KPI Report → real Slack post lands
3. Click "▶ Run now" on Territory Lookalike → CSV in outputs/ + Slack summary
4. Click "▶ Run now" on Daily Prospects → CSV + Slack summary
5. Open Run History table → show all three logged
6. Walk through one CSV to show data shape
