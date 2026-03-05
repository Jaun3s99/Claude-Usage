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

# Maps your Anthropic API key IDs → readable names shown on the dashboard.
# In Vercel, set WORKSPACE_NAMES as a single-line JSON environment variable.
# Your 6 key IDs (from Supabase) — fill in the names on the right:
#
# {
#   "apikey_01FnNDgoL3ZMVzKaTyFPhivs": "OpenClaw-2",
#   "apikey_016VHEs2Ko1r23ZJkwBK1RNF": "Rhea",
#   "apikey_01DxGT692EZf61pp3w1rzMLn": "Ivy",
#   "apikey_01RxBMSZGWbDM9bD5apdRKvW": "Beth",
#   "apikey_01KuCNK7evmwuFEXoNtdpRPt": "Paul2-api",
#   "apikey_01RgJoQTna7fToJgg73tF4hx": "Nicole-Aria"
# }
#
# NOTE: The names above are guesses based on cost order — see README for
# how to confirm which key ID maps to which name using the /api/debug endpoint.
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

    Strategy:
      1. Fetch actual daily org total from cost_report (grouped by workspace_id)
      2. Fetch per-key per-model token counts from usage_report
      3. Estimate relative costs using correct per-token-type pricing
      4. Scale estimated costs so the daily total matches the actual cost_report total
    """
    if not ANTHROPIC_ADMIN_KEY:
        raise ValueError("ANTHROPIC_ADMIN_KEY is not set.")

    headers = {
        "x-api-key": ANTHROPIC_ADMIN_KEY,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }

    starting_at = f"{start_date}T00:00:00Z"
    ending_at   = f"{end_date}T23:59:59Z"

    # ── Pricing table (input price, output price) per million tokens ───
    # Cache write = 1.25× input price; cache read = 0.10× input price
    PRICING = {
        "claude-opus-4-5-20251101":   (15.00, 75.00),
        "claude-opus-4-20250514":     (15.00, 75.00),
        "claude-sonnet-4-5-20250929": (3.00,  15.00),
        "claude-sonnet-4-20250514":   (3.00,  15.00),
        "claude-haiku-4-5-20251001":  (0.80,   4.00),
        "claude-3-5-sonnet-20241022": (3.00,  15.00),
        "claude-3-5-haiku-20241022":  (0.80,   4.00),
        "claude-3-haiku-20240307":    (0.25,   1.25),
        "claude-3-opus-20240229":     (15.00, 75.00),
    }

    def get_pricing(model):
        """Return (input_price, output_price) per million tokens.
        Falls back to pattern matching for newer model versions."""
        if model in PRICING:
            return PRICING[model]
        m = model.lower()
        if "haiku"  in m: return (0.80,  4.00)
        if "sonnet" in m: return (3.00, 15.00)
        if "opus"   in m: return (15.00, 75.00)
        return (3.00, 15.00)  # safe middle-ground default

    def estimate_cost(r, model):
        """Estimate cost using correct pricing for each token type.
        Cache read tokens cost 0.10× input price (not full price).
        Cache write tokens cost 1.25× input price."""
        in_p, out_p = get_pricing(model)
        cache_write_p = in_p * 1.25
        cache_read_p  = in_p * 0.10

        uncached   = r.get("uncached_input_tokens", 0)
        cache_5m   = r.get("cache_creation", {}).get("ephemeral_5m_input_tokens", 0)
        cache_1h   = r.get("cache_creation", {}).get("ephemeral_1h_input_tokens", 0)
        cache_read = r.get("cache_read_input_tokens", 0)
        out_tok    = r.get("output_tokens", 0)

        return (
            uncached            / 1_000_000 * in_p
            + (cache_5m + cache_1h) / 1_000_000 * cache_write_p
            + cache_read        / 1_000_000 * cache_read_p
            + out_tok           / 1_000_000 * out_p
        )

    # ── Fetch API key names ────────────────────────────────────────────
    api_key_names = fetch_api_key_names(headers)

    # ── Fetch actual daily total cost from cost_report ─────────────────
    # group_by=api_key_id is NOT supported by this endpoint.
    # group_by=workspace_id gives us org total (all workspace_ids are null here).
    cost_resp = requests.get(
        "https://api.anthropic.com/v1/organizations/cost_report",
        headers=headers,
        params=[
            ("starting_at", starting_at),
            ("ending_at",   ending_at),
            ("bucket_width", "1d"),
            ("group_by[]",  "workspace_id"),
            ("limit", 31),
        ],
        timeout=30,
    )
    # daily_actual_cost = {date: total_usd_that_day_for_whole_org}
    daily_actual_cost = {}
    if cost_resp.ok:
        for bucket in cost_resp.json().get("data", []):
            date = bucket.get("starting_at", "")[:10]
            day_total = sum(float(r.get("amount", 0)) for r in bucket.get("results", []))
            daily_actual_cost[date] = day_total

    # ── Fetch per-key per-model token usage ───────────────────────────
    usage_resp = requests.get(
        "https://api.anthropic.com/v1/organizations/usage_report/messages",
        headers=headers,
        params=[
            ("starting_at", starting_at),
            ("ending_at",   ending_at),
            ("bucket_width", "1d"),
            ("group_by[]", "model"),
            ("group_by[]", "api_key_id"),
            ("limit", 31),
        ],
        timeout=30,
    )
    if not usage_resp.ok:
        raise ValueError(f"Usage API error {usage_resp.status_code}: {usage_resp.text[:400]}")

    # ── First pass: compute estimated cost per row ─────────────────────
    rows = []
    for bucket in usage_resp.json().get("data", []):
        date = bucket.get("starting_at", start_date)[:10]
        for r in bucket.get("results", []):
            model   = r.get("model", "unknown")
            key_id  = r.get("api_key_id") or "unknown"
            est     = estimate_cost(r, model)

            uncached   = r.get("uncached_input_tokens", 0)
            cache_5m   = r.get("cache_creation", {}).get("ephemeral_5m_input_tokens", 0)
            cache_1h   = r.get("cache_creation", {}).get("ephemeral_1h_input_tokens", 0)
            cache_read = r.get("cache_read_input_tokens", 0)
            inp = uncached + cache_read + cache_5m + cache_1h
            out = r.get("output_tokens", 0)
            req = r.get("request_count", 0)

            rows.append((date, model, key_id, inp, out, req, est))

    # ── Sum estimated costs per day for proportional scaling ──────────
    daily_est_total = {}
    for (date, _, _, _, _, _, est) in rows:
        daily_est_total[date] = daily_est_total.get(date, 0) + est

    # ── Second pass: scale costs to match actual daily total ──────────
    records = []
    for (date, model, key_id, inp, out, req, est) in rows:
        actual_total = daily_actual_cost.get(date)
        est_total    = daily_est_total.get(date, 1) or 1

        if actual_total is not None and est_total > 0:
            # Anchor total to real cost, distribute proportionally by token math
            cost = est / est_total * actual_total
        else:
            cost = est  # fallback if cost_report unavailable

        key_name = WORKSPACE_NAMES.get(key_id) or api_key_names.get(key_id)
        if not key_name:
            if WORKSPACE_NAMES:
                continue  # skip unrecognised keys when whitelist is configured
            else:
                key_name = f"Key-{key_id[-6:]}"

        records.append({
            "date":           date,
            "model":          model,
            "workspace_id":   key_id,
            "workspace_name": key_name,
            "input_tokens":   inp,
            "output_tokens":  out,
            "total_tokens":   inp + out,
            "cost_usd":       round(cost, 6),
            "request_count":  req,
        })

    return records


@app.route("/api/debug")
def debug():
    """Returns raw Anthropic API responses so we can diagnose cost and key issues."""
    try:
        headers = {
            "x-api-key": ANTHROPIC_ADMIN_KEY,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        end_date   = datetime.now().strftime("%Y-%m-%d")
        start_date = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d")

        # Raw usage response (tokens)
        usage_resp = requests.get(
            "https://api.anthropic.com/v1/organizations/usage_report/messages",
            headers=headers,
            params=[
                ("starting_at", f"{start_date}T00:00:00Z"),
                ("ending_at",   f"{end_date}T23:59:59Z"),
                ("bucket_width", "1d"),
                ("group_by[]", "model"),
                ("group_by[]", "api_key_id"),
                ("limit", 31),
            ],
            timeout=30,
        )

        # Raw cost report — using workspace_id (api_key_id is not a valid group_by)
        cost_resp = requests.get(
            "https://api.anthropic.com/v1/organizations/cost_report",
            headers=headers,
            params=[
                ("starting_at", f"{start_date}T00:00:00Z"),
                ("ending_at",   f"{end_date}T23:59:59Z"),
                ("bucket_width", "1d"),
                ("group_by[]",  "workspace_id"),
                ("limit", 31),
            ],
            timeout=30,
        )

        # Raw API keys response
        keys_resp = requests.get(
            "https://api.anthropic.com/v1/api_keys",
            headers=headers,
            params={"limit": 100},
            timeout=10,
        )

        return jsonify({
            "usage_status": usage_resp.status_code,
            "usage_sample": usage_resp.json().get("data", [])[:2] if usage_resp.ok else usage_resp.text[:500],
            "cost_status": cost_resp.status_code,
            "cost_sample": cost_resp.json().get("data", [])[:2] if cost_resp.ok else cost_resp.text[:500],
            "keys_status": keys_resp.status_code,
            "keys_data": keys_resp.json() if keys_resp.ok else keys_resp.text[:500],
        })
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
