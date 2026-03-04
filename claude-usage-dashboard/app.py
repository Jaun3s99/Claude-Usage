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

# Optional: manually map workspace IDs to readable names.
# In Vercel env vars, set WORKSPACE_NAMES as JSON, e.g.:
# {"wrkspc_abc123": "Engineering", "wrkspc_def456": "Sales"}
try:
    WORKSPACE_NAMES = json.loads(os.environ.get("WORKSPACE_NAMES", "{}"))
except Exception:
    WORKSPACE_NAMES = {}


# ──────────────────────────────────────────────
# Anthropic Usage API
# ──────────────────────────────────────────────

def fetch_api_key_names(headers: dict) -> dict:
    """Fetch API key ID → name mapping from Anthropic API."""
    try:
        resp = requests.get(
            "https://api.anthropic.com/v1/api_keys",
            headers=headers,
            params={"limit": 100},
            timeout=10,
        )
        if resp.ok:
            keys = resp.json().get("data", [])
            return {k["id"]: k.get("name", k["id"]) for k in keys if "id" in k}
    except Exception:
        pass
    return {}


def fetch_anthropic_usage(start_date: str, end_date: str) -> list:
    """
    Fetch usage data from Anthropic's Admin API.
    Docs: https://docs.anthropic.com/en/api/administration

    Uses:
      GET /v1/organizations/usage_report/messages  — token counts by model/workspace
      GET /v1/organizations/cost_report            — actual USD costs
    """
    if not ANTHROPIC_ADMIN_KEY:
        raise ValueError("ANTHROPIC_ADMIN_KEY is not set.")

    headers = {
        "x-api-key": ANTHROPIC_ADMIN_KEY,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }

    # Model pricing (per million tokens) as fallback if cost API unavailable
    PRICING = {
        "claude-opus-4-5-20251101":   (15.00, 75.00),
        "claude-sonnet-4-5-20250929": (3.00,  15.00),
        "claude-haiku-4-5-20251001":  (0.80,   4.00),
        "claude-3-opus-20240229":     (15.00, 75.00),
        "claude-3-5-sonnet-20241022": (3.00,  15.00),
        "claude-3-5-haiku-20241022":  (0.80,   4.00),
        "claude-3-haiku-20240307":    (0.25,   1.25),
    }

    starting_at = f"{start_date}T00:00:00Z"
    ending_at   = f"{end_date}T23:59:59Z"

    # Fetch API key names AND workspace names from Anthropic
    api_key_names = fetch_api_key_names(headers)
    workspace_names = {}
    try:
        ws_resp = requests.get(
            "https://api.anthropic.com/v1/workspaces",
            headers=headers, params={"limit": 100}, timeout=10,
        )
        if ws_resp.ok:
            workspace_names = {w["id"]: w["name"] for w in ws_resp.json().get("data", []) if "id" in w}
    except Exception:
        pass

    # ── Fetch token usage grouped by workspace + api_key ──────────
    usage_resp = requests.get(
        "https://api.anthropic.com/v1/organizations/usage_report/messages",
        headers=headers,
        params=[
            ("starting_at", starting_at),
            ("ending_at",   ending_at),
            ("bucket_width", "1d"),
            ("group_by[]", "model"),
            ("group_by[]", "workspace_id"),
            ("group_by[]", "api_key_id"),
            ("limit", 31),
        ],
        timeout=30,
    )
    if not usage_resp.ok:
        raise ValueError(f"Usage API error {usage_resp.status_code}: {usage_resp.text[:400]}")

    # ── Normalise into our flat record format ──────────────────────
    records = []
    for bucket in usage_resp.json().get("data", []):
        date = bucket.get("starting_at", start_date)[:10]
        for r in bucket.get("results", []):
            model  = r.get("model", "unknown")
            ws_id  = r.get("workspace_id") or "default"
            key_id = r.get("api_key_id") or "unknown"

            inp = r.get("uncached_input_tokens", 0) + r.get("cache_read_input_tokens", 0)
            out = r.get("output_tokens", 0)

            # Cost per model using pricing table
            in_p, out_p = PRICING.get(model, (3.00, 15.00))
            for pkey, prices in PRICING.items():
                if model.startswith(pkey.rsplit("-", 1)[0]):
                    in_p, out_p = prices
                    break
            cost = (inp / 1_000_000 * in_p) + (out / 1_000_000 * out_p)

            # Person name resolution:
            # - Laura/Nicolette/Megan have their own workspace → use workspace name
            # - Nicole/Paul/Juan share default workspace → use API key name
            ws_name = workspace_names.get(ws_id, "")
            is_default_ws = (ws_name.lower() in ("default", "") or ws_id == "default")

            if is_default_ws:
                # Distinguish by API key name (Nicole, Paul, Juan)
                person_name = (
                    WORKSPACE_NAMES.get(key_id)
                    or api_key_names.get(key_id)
                    or f"Key {key_id[:8]}"
                )
                unique_id = key_id  # use key_id so each person gets their own row
            else:
                # Own workspace (Laura, Nicolette, Megan)
                person_name = (
                    WORKSPACE_NAMES.get(ws_id)
                    or ws_name
                    or ws_id
                )
                unique_id = ws_id

            records.append({
                "date":           date,
                "model":          model,
                "workspace_id":   unique_id,
                "workspace_name": person_name,
                "input_tokens":   inp,
                "output_tokens":  out,
                "total_tokens":   inp + out,
                "cost_usd":       round(cost, 6),
                "request_count":  r.get("request_count", 0),
            })

    return records


@app.route("/api/workspaces")
def list_workspaces():
    """Shows raw workspace IDs from Anthropic — use these to set WORKSPACE_NAMES in Vercel."""
    try:
        headers = {
            "x-api-key": ANTHROPIC_ADMIN_KEY,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        resp = requests.get(
            "https://api.anthropic.com/v1/workspaces",
            headers=headers,
            params={"limit": 100},
            timeout=10,
        )
        if resp.ok:
            return jsonify({"workspaces": resp.json().get("data", []), "manual_mapping": WORKSPACE_NAMES})
        return jsonify({"error": f"HTTP {resp.status_code}: {resp.text[:200]}", "manual_mapping": WORKSPACE_NAMES})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


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
        return jsonify({"success": False, "error": str(e), "hint": "Check your ANTHROPIC_ADMIN_KEY and visit https://docs.anthropic.com/en/api/administration for the correct endpoint"}), 500


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
