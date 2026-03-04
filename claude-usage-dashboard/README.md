# Claude API Usage Dashboard
### Daversa Partners · Built with Python + Supabase + Vercel

A real-time dashboard for tracking Claude API token usage, costs, and team breakdowns — accessible by anyone with the URL.

---

## What It Shows

- **Total tokens** consumed (input + output), with per-day averages
- **Total cost** in USD for any date range
- **API request volume** over time
- **Usage by model** (Opus, Sonnet, Haiku) — pie chart + table
- **Usage by team/workspace** — bar chart + table
- **Daily trends** with cost overlay line chart

---

## Tech Stack (same as Jeff's World Cup dashboard)

| Layer | Tool |
|-------|------|
| Backend | Python + Flask |
| Database | Supabase (PostgreSQL) |
| Hosting | Vercel |
| Data Source | Anthropic Admin API |
| Frontend | Vanilla HTML + Chart.js |

---

## Setup (5 Steps)

### Step 1 — Create a Supabase Project

1. Go to [supabase.com](https://supabase.com) and create a free account
2. Create a new project (save your database password)
3. Go to **SQL Editor → New Query**
4. Paste the entire contents of `schema.sql` and click **Run**
5. Copy your keys from **Project Settings → API**:
   - `Project URL` → `SUPABASE_URL`
   - `anon public` key → `SUPABASE_ANON_KEY`
   - `service_role` key → `SUPABASE_SERVICE_KEY`

### Step 2 — Get Your Anthropic Admin API Key

1. Go to [console.anthropic.com](https://console.anthropic.com)
2. Navigate to **Settings → API Keys**
3. Create a new key with **Admin** scope
4. Copy it → `ANTHROPIC_ADMIN_KEY`

> **Note:** This is a different key from your regular Claude API key.
> You need an Admin key to access usage/billing data.
> Check [docs.anthropic.com](https://docs.anthropic.com/en/api/administration) for the latest usage API endpoint details.

### Step 3 — Deploy to Vercel

1. Push this project to a GitHub repository
2. Go to [vercel.com](https://vercel.com) → **New Project** → import your repo
3. Add these **Environment Variables** in Vercel settings:
   ```
   SUPABASE_URL
   SUPABASE_ANON_KEY
   SUPABASE_SERVICE_KEY
   ANTHROPIC_ADMIN_KEY
   USE_MOCK_DATA=false
   ```
4. Click **Deploy** — Vercel will auto-detect the Python app

### Step 4 — Test with Mock Data First

While you're confirming your Anthropic API key works, set `USE_MOCK_DATA=true`
in Vercel environment variables. The dashboard will show realistic generated data
so you can verify everything looks right before connecting the real API.

### Step 5 — Enable Auto-Sync

The `vercel.json` already includes a daily cron job:
```json
"crons": [{"path": "/api/sync", "schedule": "0 6 * * *"}]
```
This calls `/api/sync` every day at 6am UTC, pulling the latest 7 days of data
from Anthropic and storing it in Supabase. You can also click **Sync Now**
on the dashboard at any time.

---

## Running Locally

```bash
# 1. Clone the repo
git clone https://github.com/YOUR_ORG/claude-usage-dashboard
cd claude-usage-dashboard

# 2. Install dependencies
pip install -r requirements.txt

# 3. Set up environment variables
cp .env.example .env
# → Fill in your values in .env

# 4. Start the server
python app.py

# 5. Open your browser
open http://localhost:5000
```

---

## File Structure

```
claude-usage-dashboard/
├── app.py              ← Flask backend (API routes + Anthropic sync)
├── requirements.txt    ← Python dependencies
├── vercel.json         ← Vercel deployment + cron schedule
├── schema.sql          ← Supabase database setup
├── .env.example        ← Environment variable template
├── static/
│   └── index.html      ← Dashboard UI (Chart.js, no framework needed)
└── README.md
```

---

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Dashboard UI |
| `/api/usage` | GET | Usage data (`start_date`, `end_date` params) |
| `/api/sync` | GET/POST | Trigger Anthropic → Supabase sync |
| `/api/health` | GET | Health check |

---

## Customization

**Change the branding:** Edit the `header` section in `static/index.html` — swap "DP" and "Daversa Partners" for your logo/name.

**Add more metrics:** Add columns to `usage_records` in `schema.sql`, update the sync logic in `app.py`, and add a new chart in `index.html`.

**Make it private:** Change the Supabase RLS policies in `schema.sql` to require authentication, then add a login page.

**Add email alerts:** Use Supabase's built-in webhooks or add a `/api/alert` endpoint to send email when daily cost exceeds a threshold.

---

## Anthropic Usage API Notes

The dashboard calls Anthropic's Admin API to fetch usage. The endpoint used is:
```
GET https://api.anthropic.com/v1/organizations/usage
```

If this endpoint returns an error, check:
- [docs.anthropic.com/en/api/administration](https://docs.anthropic.com/en/api/administration) for the latest endpoint
- That your API key has Admin scope (not just regular API scope)
- The `fetch_anthropic_usage()` function in `app.py` — it may need small adjustments to match Anthropic's current response format

---

*Built for Daversa Partners · Inspired by Jeff Liaw's WC2026 simulator*
