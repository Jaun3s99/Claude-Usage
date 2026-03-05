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
# Your 6 key IDs:
# {
#   "apikey_01FnNDgoL3ZMVzKaTyFPhivs": "OpenClaw-2",
#   "apikey_016VHEs2Ko1r23ZJkwBK1RNF": "Rhea",
#   "apikey_01DxGT692EZf61pp3w1rzMLn": "Ivy",
#   "apikey_01RxBMSZGWbDM9bD5apdRKvW": "Beth",
#   "apikey_01KuCNK7evmwuFEXoNtdpRPt": "Paul2-api",
#   "apikey_01RgJoQTna7fToJgg73tF4hx": "Nicole-Aria"
# }
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

    Strategy:
      1. Fetch cost_report grouped by "description" — this gives actual billed cost per
         model per token-type per day for the whole org.  The `amount` field is in cents
         (divide by 100).  This is the ground truth for billing.

      2. Fetch usage_report grouped by api_key_id+model — gives token counts per key.

      3. For each day, aggregate org-level tokens per model+token_type from usage_report.

      4. Compute implied price per token:
           price = org_cost[model][token_type] / org_tokens[model][token_type]
         This extracts the ACTUAL per-token price Anthropic charged, even if it differs
         from published list prices (volume discounts, new models, etc.).

      5. Apply implied prices to per-key token counts → exact per-key daily cost.

    This approach matches Anthropic's billing exactly because we use their own per-token
    costs as the pricing source rather than hard-coded estimates.
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

    # ── Fallback pricing table (used only if cost_description unavailable) ──
    # These are approximate published list prices per million tokens.
    # The description-based approach above is preferred and more accurate.
    FALLBACK_PRICING = {
        "claude-opus-4-5-20251101":   (5.00,  25.00),  # actual observed price
        "claude-opus-4-20250514":     (15.00, 75.00),
        "claude-opus-4-6":            (5.00,  25.00),  # actual observed price
        "claude-sonnet-4-6":          (3.00,  15.00),
        "claude-sonnet-4-5-20250929": (3.00,  15.00),
        "claude-sonnet-4-20250514":   (3.00,  15.00),
        "claude-haiku-4-5-20251001":  (0.80,   4.00),
        "claude-3-5-sonnet-20241022": (3.00,  15.00),
        "claude-3-5-haiku-20241022":  (0.80,   4.00),
        "claude-3-haiku-20240307":    (0.25,   1.25),
        "claude-3-opus-20240229":     (15.00, 75.00),
    }

    def get_fallback_pricing(model):
        if model in FALLBACK_PRICING:
            return FALLBACK_PRICING[model]
        m = model.lower()
        # 4-series Opus is cheaper than 3-series
        if "opus-4" in m: return (5.00,  25.00)
        if "opus"   in m: return (15.00, 75.00)
        if "haiku"  in m: return (0.80,   4.00)
        if "sonnet" in m: return (3.00,  15.00)
        return (3.00, 15.00)

    # ── Fetch API key names ────────────────────────────────────────────
    api_key_names = fetch_api_key_names(headers)

    # ── Step 1: Fetch actual costs by model+token_type from cost_report ───
    # cost_by_model[date][model][token_type] = cost_usd
    cost_by_model   = {}
    daily_act_cost  = {}   # fallback: org total per day
    use_description = False

    cost_resp = requests.get(
        "https://api.anthropic.com/v1/organizations/cost_report",
        headers=headers,
        params=[
            ("starting_at", starting_at),
            ("ending_at",   ending_at),
            ("bucket_width", "1d"),
            ("group_by[]",  "description"),
            ("limit", 31),
        ],
        timeout=30,
    )
    if cost_resp.ok:
        use_description = True
        for bucket in cost_resp.json().get("data", []):
            date = bucket.get("starting_at", "")[:10]
            cost_by_model[date] = {}
            for r in bucket.get("results", []):
                model      = r.get("model") or "unknown"
                token_type = r.get("token_type") or "unknown"
                # amount is in cents — divide by 100 to get USD
                amount_usd = float(r.get("amount", 0)) / 100
                if model not in cost_by_model[date]:
                    cost_by_model[date][model] = {}
                cost_by_model[date][model][token_type] = (
                    cost_by_model[date][model].get(token_type, 0) + amount_usd
                )
            # Store org total for this day
            daily_act_cost[date] = sum(
                v
                for model_data in cost_by_model[date].values()
                for v in model_data.values()
            )
    else:
        # Fallback: org total from workspace_id grouping
        for group_by_val in ("workspace_id",):
            resp2 = requests.get(
                "https://api.anthropic.com/v1/organizations/cost_report",
                headers=headers,
                params=[
                    ("starting_at", starting_at),
                    ("ending_at",   ending_at),
                    ("bucket_width", "1d"),
                    ("group_by[]",  group_by_val),
                    ("limit", 31),
                ],
                timeout=30,
            )
            if resp2.ok:
                for bucket in resp2.json().get("data", []):
                    date = bucket.get("starting_at", "")[:10]
                    daily_act_cost[date] = sum(
                        float(r.get("amount", 0)) / 100
                        for r in bucket.get("results", [])
                    )
                break

    # ── Step 2: Fetch per-key per-model token usage ───────────────────
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

    # ── Step 3: Parse usage rows + build org-level token aggregates ────
    # org_tokens[date][model][token_type] = total_org_tokens
    org_tokens = {}
    rows = []

    for bucket in usage_resp.json().get("data", []):
        date = bucket.get("starting_at", start_date)[:10]
        for r in bucket.get("results", []):
            model   = r.get("model", "unknown")
            key_id  = r.get("api_key_id") or "unknown"

            uncached    = r.get("uncached_input_tokens", 0)
            cache_write = (
                r.get("cache_creation", {}).get("ephemeral_5m_input_tokens", 0)
                + r.get("cache_creation", {}).get("ephemeral_1h_input_tokens", 0)
            )
            cache_read  = r.get("cache_read_input_tokens", 0)
            out_tok     = r.get("output_tokens", 0)
            req         = r.get("request_count", 0)

            inp = uncached + cache_read + cache_write

            # Accumulate org-level token totals
            if date not in org_tokens:
                org_tokens[date] = {}
            if model not in org_tokens[date]:
                org_tokens[date][model] = {
                    "uncached_input_tokens": 0,
                    "cache_creation.ephemeral_5m_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                    "output_tokens": 0,
                }
            org_tokens[date][model]["uncached_input_tokens"]                    += uncached
            org_tokens[date][model]["cache_creation.ephemeral_5m_input_tokens"] += cache_write
            org_tokens[date][model]["cache_read_input_tokens"]                  += cache_read
            org_tokens[date][model]["output_tokens"]                            += out_tok

            rows.append((date, model, key_id, uncached, cache_write, cache_read, out_tok, inp, out_tok, req))

    # ── Step 4: Compute per-key costs using implied prices ─────────────
    # If description data available: cost = key_tokens / org_tokens * org_cost
    # Otherwise: proportional from org total using fallback pricing
    records = []

    # Pre-compute estimated costs for fallback proportional method
    daily_est_total = {}
    for (date, model, key_id, uncached, cache_write, cache_read, out_tok, inp, out, req) in rows:
        in_p, out_p = get_fallback_pricing(model)
        est = (
            uncached    / 1e6 * in_p
            + cache_write / 1e6 * in_p * 1.25
            + cache_read  / 1e6 * in_p * 0.10
            + out_tok     / 1e6 * out_p
        )
        daily_est_total[date] = daily_est_total.get(date, 0) + est

    # Per-row cost calculation
    for (date, model, key_id, uncached, cache_write, cache_read, out_tok, inp, out, req) in rows:

        if use_description and date in cost_by_model and model in cost_by_model[date]:
            # ── Best path: exact per-token cost from Anthropic billing ──
            # Distribute each token-type's org cost proportionally by key token count
            m_cost   = cost_by_model[date][model]
            m_tokens = org_tokens.get(date, {}).get(model, {})
            cost = 0.0

            token_map = [
                ("uncached_input_tokens",                    uncached),
                ("cache_creation.ephemeral_5m_input_tokens", cache_write),
                ("cache_read_input_tokens",                  cache_read),
                ("output_tokens",                            out_tok),
            ]
            for tt, key_tok in token_map:
                org_tok  = m_tokens.get(tt, 0)
                org_cost = m_cost.get(tt, 0)
                if org_tok > 0 and key_tok > 0:
                    cost += key_tok / org_tok * org_cost
                # If org_tok == 0 or key_tok == 0, contribution is $0

        else:
            # ── Fallback: proportional from org daily total ──────────────
            in_p, out_p = get_fallback_pricing(model)
            est = (
                uncached    / 1e6 * in_p
                + cache_write / 1e6 * in_p * 1.25
                + cache_read  / 1e6 * in_p * 0.10
                + out_tok     / 1e6 * out_p
            )
            actual_total = daily_act_cost.get(date)
            est_total    = daily_est_total.get(date, 1) or 1
            cost = (est / est_total * actual_total) if actual_total else est

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
        cost_desc_resp = requests.get(
            "https://api.anthropic.com/v1/organizations/cost_report",
            headers=headers,
            params=[
                ("starting_at", f"{start_date}T00:00:00Z"),
                ("ending_at",   f"{end_date}T23:59:59Z"),
                ("bucket_width", "1d"),
                ("group_by[]",  "description"),
                ("limit", 31),
            ],
            timeout=30,
        )
        cost_ws_resp = requests.get(
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

        # Compute what the sync would estimate for the debug window
        try:
            est_records = fetch_anthropic_usage(start_date, end_date)
            est_summary = {}
            for r in est_records:
                w = r["workspace_name"]
                est_summary[w] = round(est_summary.get(w, 0) + r["cost_usd"], 4)
        except Exception as e:
            est_summary = {"error": str(e)}

        return jsonify({
            "usage_status": usage_resp.status_code,
            "usage_sample": usage_resp.json().get("data", [])[:2] if usage_resp.ok else usage_resp.text[:500],
            "cost_description_status": cost_desc_resp.status_code,
            "cost_description_sample": cost_desc_resp.json().get("data", [])[:2] if cost_desc_resp.ok else cost_desc_resp.text[:500],
            "cost_workspace_status": cost_ws_resp.status_code,
            "cost_workspace_sample": cost_ws_resp.json().get("data", [])[:2] if cost_ws_resp.ok else cost_ws_resp.text[:500],
            "estimated_cost_by_key_last_3d": est_summary,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def generate_mock_usage(start_date: str, end_date: str) -> list:
    import random
    random.seed(42)
    start = datetime.strptime(start_date, "%Y-%m-%d")
    end   = datetime.strptime(end_date,   "%Y-%m-%d")
    models   = ["claude-opus-4-6", "claude-sonnet-4-6", "claude-haiku-4-5-20251001"]
    pricing  = {"claude-opus-4-6": (5.00, 25.00), "claude-sonnet-4-6": (3.00, 15.00), "claude-haiku-4-5-20251001": (0.80, 4.00)}
    workspaces = [
        {"id": "ws_001", "name": "OpenClaw-2"},
        {"id": "ws_002", "name": "Rhea"},
        {"id": "ws_003", "name": "Ivy"},
        {"id": "ws_004", "name": "Beth"},
    ]
    records = []
    current = start
    while current <= end:
        vm = 1.0 if current.weekday() < 5 else 0.2
        for model in models:
            for ws in workspaces:
                reqs = int(random.randint(30, 300) * vm)
                if not reqs: continue
                inp = reqs * random.randint(400, 1800)
                out = reqs * random.randint(150, 600)
                ip, op = pricing[model]
                cost = inp / 1e6 * ip + out / 1e6 * op
                records.append({
                    "date": current.strftime("%Y-%m-%d"), "model": model,
                    "workspace_id": ws["id"], "workspace_name": ws["name"],
                    "input_tokens": inp, "output_tokens": out,
                    "total_tokens": inp + out, "cost_usd": round(cost, 6),
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
    try:
        days_back  = int(request.args.get("days_back", 7))
        end_date   = datetime.now().strftime("%Y-%m-%d")
        start_date = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")

        records = generate_mock_usage(start_date, end_date) if USE_MOCK_DATA else fetch_anthropic_usage(start_date, end_date)

        supabase = get_supabase_client(use_service_key=True)
        synced, errors = 0, []

        for r in records:
            try:
                supabase.table("usage_records").upsert(
                    {**r, "updated_at": datetime.utcnow().isoformat()},
                    on_conflict="date,model,workspace_id"
                ).execute()
                synced += 1
            except Exception as e:
                errors.append(str(e))

        supabase.table("sync_log").insert({
            "sync_date": end_date,
            "status": "success" if not errors else "partial",
            "records_synced": synced,
            "error_message": "; ".join(errors[:3]) if errors else None,
        }).execute()

        return jsonify({
            "success": True, "records_synced": synced,
            "start_date": start_date, "end_date": end_date,
            "mode": "mock" if USE_MOCK_DATA else "live",
            "errors": errors[:3],
        })

    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/usage")
def usage():
    try:
        end_date   = request.args.get("end_date",   datetime.now().strftime("%Y-%m-%d"))
        start_date = request.args.get("start_date", (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d"))

        supabase = get_supabase_client()
        result   = (
            supabase.table("usage_records")
            .select("*")
            .gte("date", start_date)
            .lte("date", end_date)
            .order("date", desc=False)
            .execute()
        )
        records = result.data or []

        daily, by_model, by_workspace = {}, {}, {}
        for r in records:
            d, m, w = r["date"], r["model"], r.get("workspace_name", "Unknown")
            for bucket, key in [(daily, d), (by_model, m), (by_workspace, w)]:
                if key not in bucket:
                    bucket[key] = {"label": key, "input_tokens": 0, "output_tokens": 0,
                                   "total_tokens": 0, "cost_usd": 0.0, "request_count": 0}
                bucket[key]["input_tokens"]  += r.get("input_tokens", 0)
                bucket[key]["output_tokens"] += r.get("output_tokens", 0)
                bucket[key]["total_tokens"]  += r.get("total_tokens", 0)
                bucket[key]["cost_usd"]      += float(r.get("cost_usd", 0))
                bucket[key]["request_count"] += r.get("request_count", 0)

        totals = {
            "input_tokens":  sum(v["input_tokens"]  for v in daily.values()),
            "output_tokens": sum(v["output_tokens"] for v in daily.values()),
            "total_tokens":  sum(v["total_tokens"]  for v in daily.values()),
            "cost_usd":      round(sum(v["cost_usd"] for v in daily.values()), 2),
            "request_count": sum(v["request_count"] for v in daily.values()),
        }

        return jsonify({
            "totals": totals,
            "daily": sorted(daily.values(), key=lambda x: x["label"]),
            "by_model": sorted(by_model.values(), key=lambda x: x["cost_usd"], reverse=True),
            "by_workspace": sorted(by_workspace.values(), key=lambda x: x["cost_usd"], reverse=True),
            "start_date": start_date, "end_date": end_date,
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/health")
def health():
    return jsonify({"status": "ok", "timestamp": datetime.utcnow().isoformat()})


if __name__ == "__main__":
    app.run(debug=True, port=5000)
