"""
Claude API Usage Dashboard - Flask Backend
==========================================
Stack: Python + Flask + Supabase + Vercel
Deployed by: Daversa Partners
"""

import os
import json
import requests
from datetime import datetime, timedelta
from flask import Flask, jsonify, request, send_from_directory
from supabase import create_client, Client
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__, static_folder="static", static_url_path="")

# ──────────────────────────────────────────────
# Environment Variables
# ──────────────────────────────────────────────
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")
ANTHROPIC_ADMIN_KEY = os.environ.get("ANTHROPIC_ADMIN_KEY")

# Set USE_MOCK_DATA=true in .env to use generated data while you're getting
# the real API connected. Great for testing your dashboard design first.
USE_MOCK_DATA = os.environ.get("USE_MOCK_DATA", "true").lower() == "true"


# ──────────────────────────────────────────────
# Anthropic Usage API
# ──────────────────────────────────────────────

def fetch_anthropic_usage(start_date: str, end_date: str) -> list:
    """
    Fetch usage data from Anthropic's Admin API.

    Docs: https://docs.anthropic.com/en/api/administration
    Endpoint: GET https://api.anthropic.com/v1/organizations/usage

    Requires an Admin API key (not a regular API key).
    Create one at: https://console.anthropic.com → Settings → API Keys

    The response will include per-model, per-workspace token counts and costs.
    """
    if not ANTHROPIC_ADMIN_KEY:
        raise ValueError("ANTHROPIC_ADMIN_KEY is not set. Add it to your .env file.")

    headers = {
        "x-api-key": ANTHROPIC_ADMIN_KEY,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }

    # NOTE: Verify this endpoint at https://docs.anthropic.com
    # The admin usage endpoint may require specific org/workspace parameters.
    url = "https://api.anthropic.com/v1/organizations/usage"
    params = {
        "start_time": f"{start_date}T00:00:00Z",
        "end_time": f"{end_date}T23:59:59Z",
    }

    response = requests.get(url, headers=headers, params=params, timeout=30)
    response.raise_for_status()
    data = response.json()

    # Normalize response into our standard format
    records = []
    for item in data.get("data", []):
        records.append({
            "date": item.get("date", start_date),
            "model": item.get("model", "unknown"),
            "workspace_id": item.get("workspace_id", "default"),
            "workspace_name": item.get("workspace_name", "Default"),
            "input_tokens": item.get("input_tokens", 0),
            "output_tokens": item.get("output_tokens", 0),
            "total_tokens": item.get("input_tokens", 0) + item.get("output_tokens", 0),
            "cost_usd": item.get("cost_usd", 0),
            "request_count": item.get("request_count", 0),
        })
    return records


def generate_mock_usage(start_date: str, end_date: str) -> list:
    """
    Generates realistic mock data for dashboard testing.
    Switch to fetch_anthropic_usage() once your API key is connected.
    """
    import random
    random.seed(42)

    start = datetime.strptime(start_date, "%Y-%m-%d")
    end = datetime.strptime(end_date, "%Y-%m-%d")

    models = [
        "claude-opus-4-5-20251101",
        "claude-sonnet-4-5-20250929",
        "claude-haiku-4-5-20251001",
    ]
    pricing = {
        "claude-opus-4-5-20251101":   (15.00, 75.00),
        "claude-sonnet-4-5-20250929": (3.00,  15.00),
        "claude-haiku-4-5-20251001":  (0.80,   4.00),
    }
    workspaces = [
        {"id": "ws_001", "name": "Engineering"},
        {"id": "ws_002", "name": "Product"},
        {"id": "ws_003", "name": "Sales"},
        {"id": "ws_004", "name": "Marketing"},
    ]

    records = []
    current = start
    while current <= end:
        # Simulate weekday vs weekend traffic
        is_weekday = current.weekday() < 5
        volume_mult = 1.0 if is_weekday else 0.2

        for model in models:
            for ws in workspaces:
                reqs = int(random.randint(30, 300) * volume_mult)
                if reqs == 0:
                    continue
                inp = reqs * random.randint(400, 1800)
                out = reqs * random.randint(150, 600)
                in_price, out_price = pricing[model]
                cost = (inp / 1_000_000 * in_price) + (out / 1_000_000 * out_price)

                records.append({
                    "date": current.strftime("%Y-%m-%d"),
                    "model": model,
                    "workspace_id": ws["id"],
                    "workspace_name": ws["name"],
                    "input_tokens": inp,
                    "output_tokens": out,
                    "total_tokens": inp + out,
                    "cost_usd": round(cost, 6),
                    "request_count": reqs,
                })
        current += timedelta(days=1)

    return records


# ──────────────────────────────────────────────
# Supabase Helpers
# ──────────────────────────────────────────────

def get_supabase_client(use_service_key: bool = False) -> Client:
    key = SUPABASE_SERVICE_KEY if use_service_key else SUPABASE_ANON_KEY
    return create_client(SUPABASE_URL, key)


# ──────────────────────────────────────────────
# Routes
# ──────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/api/sync", methods=["GET", "POST"])
def sync():
    """
    Trigger a sync from Anthropic API → Supabase.

    Query params:
      - days_back: how many days to sync (default 7)

    Add this as a Vercel Cron Job to run automatically:
    In vercel.json, add:
      "crons": [{"path": "/api/sync", "schedule": "0 6 * * *"}]
    """
    try:
        days_back = int(request.args.get("days_back", 7))
        end_date = datetime.now().strftime("%Y-%m-%d")
        start_date = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")

        # Fetch data
        if USE_MOCK_DATA:
            records = generate_mock_usage(start_date, end_date)
        else:
            records = fetch_anthropic_usage(start_date, end_date)

        # Store in Supabase
        supabase = get_supabase_client(use_service_key=True)
        synced = 0
        errors = []

        for r in records:
            try:
                supabase.table("usage_records").upsert(
                    {**r, "updated_at": datetime.utcnow().isoformat()},
                    on_conflict="date,model,workspace_id"
                ).execute()
                synced += 1
            except Exception as e:
                errors.append(str(e))

        # Log sync
        supabase.table("sync_log").insert({
            "sync_date": end_date,
            "status": "success" if not errors else "partial",
            "records_synced": synced,
            "error_message": "; ".join(errors[:3]) if errors else None,
        }).execute()

        return jsonify({
            "success": True,
            "records_synced": synced,
            "start_date": start_date,
            "end_date": end_date,
            "mode": "mock" if USE_MOCK_DATA else "live",
            "errors": errors[:3],
        })

    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/usage")
def usage():
    """
    Return aggregated usage data for the dashboard.

    Query params:
      - start_date: YYYY-MM-DD (default: 30 days ago)
      - end_date:   YYYY-MM-DD (default: today)
    """
    try:
        end_date = request.args.get("end_date", datetime.now().strftime("%Y-%m-%d"))
        start_date = request.args.get(
            "start_date",
            (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
        )

        supabase = get_supabase_client()
        result = (
            supabase.table("usage_records")
            .select("*")
            .gte("date", start_date)
            .lte("date", end_date)
            .order("date", desc=False)
            .execute()
        )
        records = result.data or []

        # Aggregate by date
        daily = {}
        by_model = {}
        by_workspace = {}

        for r in records:
            d = r["date"]
            m = r["model"]
            w = r.get("workspace_name", "Unknown")

            for bucket, key in [(daily, d), (by_model, m), (by_workspace, w)]:
                if key not in bucket:
                    bucket[key] = {
                        "label": key,
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "total_tokens": 0,
                        "cost_usd": 0.0,
                        "request_count": 0,
                    }
                bucket[key]["input_tokens"]  += r.get("input_tokens", 0)
                bucket[key]["output_tokens"] += r.get("output_tokens", 0)
                bucket[key]["total_tokens"]  += r.get("total_tokens", 0)
                bucket[key]["cost_usd"]      += float(r.get("cost_usd", 0))
                bucket[key]["request_count"] += r.get("request_count", 0)

        # Totals
        totals = {
            "input_tokens": sum(v["input_tokens"] for v in daily.values()),
            "output_tokens": sum(v["output_tokens"] for v in daily.values()),
            "total_tokens": sum(v["total_tokens"] for v in daily.values()),
            "cost_usd": round(sum(v["cost_usd"] for v in daily.values()), 2),
            "request_count": sum(v["request_count"] for v in daily.values()),
        }

        return jsonify({
            "totals": totals,
            "daily": sorted(daily.values(), key=lambda x: x["label"]),
            "by_model": sorted(by_model.values(), key=lambda x: x["cost_usd"], reverse=True),
            "by_workspace": sorted(by_workspace.values(), key=lambda x: x["cost_usd"], reverse=True),
            "start_date": start_date,
            "end_date": end_date,
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/health")
def health():
    return jsonify({"status": "ok", "timestamp": datetime.utcnow().isoformat()})


# ──────────────────────────────────────────────
# Local dev entry point
# ──────────────────────────────────────────────
if __name__ == "__main__":
    app.run(debug=True, port=5000)
