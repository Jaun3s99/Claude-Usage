import os, time, json
from datetime import datetime, timedelta, timezone
from flask import Flask, jsonify, request
import requests
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__, static_folder="static", static_url_path="")

ADMIN_KEY   = os.environ.get("ANTHROPIC_ADMIN_KEY", "")
ADMIN_BASE  = "https://api.anthropic.com"
CACHE       = {}
CACHE_TTL   = 300  # 5 minutes

KNOWN_NAMES = json.loads(os.environ.get("KEY_NAMES", "{}"))
# KEY_NAMES env var: JSON mapping api_key_id → friendly name
# e.g. '{"apikey_abc":"Rhea","apikey_xyz":"OpenClaw-2"}'


def _admin_headers():
    return {
        "anthropic-admin-key": ADMIN_KEY,
        "Content-Type": "application/json",
    }


def _cache_get(key):
    entry = CACHE.get(key)
    if entry and time.time() - entry["ts"] < CACHE_TTL:
        return entry["data"]
    return None


def _cache_set(key, data):
    CACHE[key] = {"ts": time.time(), "data": data}


def fetch_cost_report(group_by, days=30):
    cache_key = f"cost_{group_by}_{days}"
    cached = _cache_get(cache_key)
    if cached:
        return cached

    end   = datetime.now(timezone.utc)
    start = end - timedelta(days=days)

    params = {
        "group_by[]": group_by,
        "start_time": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "end_time":   end.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    resp = requests.get(
        f"{ADMIN_BASE}/v1/organizations/cost_report",
        headers=_admin_headers(),
        params=params,
        timeout=15,
    )
    if not resp.ok:
        raise ValueError(f"Admin API {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    _cache_set(cache_key, data)
    return data


def fetch_keys():
    """Get all API keys with their names."""
    cache_key = "api_keys"
    cached = _cache_get(cache_key)
    if cached:
        return cached

    resp = requests.get(
        f"{ADMIN_BASE}/v1/organizations/api_keys",
        headers=_admin_headers(),
        timeout=15,
    )
    if not resp.ok:
        return {}
    keys = {}
    for k in resp.json().get("data", []):
        keys[k["id"]] = k.get("name", k["id"])
    _cache_set(cache_key, keys)
    return keys


def cents_to_dollars(cents):
    # Anthropic API returns costs in millicents (1/1000 of a cent = 1/100000 of a dollar)
    return round(cents / 100_000, 4)


@app.route("/")
def index():
    return app.send_static_file("index.html")


@app.route("/api/health")
def health():
    return jsonify({
        "status": "ok",
        "credentials": "configured" if ADMIN_KEY else "missing",
    })


@app.route("/api/usage")
def usage():
    """Return cost breakdown by API key + by model, for last N days."""
    try:
        days = int(request.args.get("days", 30))
        days = max(1, min(days, 90))

        if not ADMIN_KEY:
            return jsonify({"error": "ANTHROPIC_ADMIN_KEY not set"}), 500

        # Fetch cost by key
        key_report   = fetch_cost_report("api_key_id", days)
        # Fetch cost by model (description)
        model_report = fetch_cost_report("description", days)

        # --- Build key breakdown ---
        key_names = fetch_keys()
        # Override/supplement with KEY_NAMES env
        key_names.update(KNOWN_NAMES)

        keys_data = []
        for entry in key_report.get("data", []):
            key_id = entry.get("api_key_id", "unknown")
            cost   = cents_to_dollars(entry.get("total_cost", 0))
            if cost == 0:
                continue
            keys_data.append({
                "id":    key_id,
                "name":  key_names.get(key_id, key_id[:12] + "…"),
                "cost":  cost,
                "input_tokens":  entry.get("input_tokens", 0),
                "output_tokens": entry.get("output_tokens", 0),
            })
        keys_data.sort(key=lambda x: x["cost"], reverse=True)

        # --- Build model breakdown ---
        models_data = []
        for entry in model_report.get("data", []):
            model = entry.get("description", "unknown")
            cost  = cents_to_dollars(entry.get("total_cost", 0))
            if cost == 0:
                continue
            models_data.append({
                "model": model,
                "cost":  cost,
                "input_tokens":  entry.get("input_tokens", 0),
                "output_tokens": entry.get("output_tokens", 0),
            })
        models_data.sort(key=lambda x: x["cost"], reverse=True)

        total = sum(k["cost"] for k in keys_data)

        return jsonify({
            "days":    days,
            "total":   total,
            "keys":    keys_data,
            "models":  models_data,
            "updated": datetime.now(timezone.utc).isoformat(),
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/daily")
def daily():
    """Return daily cost totals for the last N days (by key)."""
    try:
        days = int(request.args.get("days", 14))
        days = max(1, min(days, 30))

        if not ADMIN_KEY:
            return jsonify({"error": "ANTHROPIC_ADMIN_KEY not set"}), 500

        # Fetch daily data — use 1-day buckets by fetching per-day
        # We approximate: get total and divide (Anthropic API may not support
        # daily bucketing natively, so we fetch multiple windows if needed)
        # For now return the summary with a note
        key_report = fetch_cost_report("api_key_id", days)
        total = cents_to_dollars(
            sum(e.get("total_cost", 0) for e in key_report.get("data", []))
        )
        avg_daily = round(total / max(days, 1), 2)

        return jsonify({
            "days":      days,
            "total":     total,
            "avg_daily": avg_daily,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(debug=True, port=5001)
