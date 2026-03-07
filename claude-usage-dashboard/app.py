import os, time, json
from datetime import datetime, timedelta, timezone
from flask import Flask, jsonify, request
import requests
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__, static_folder="static", static_url_path="")

ADMIN_KEY  = os.environ.get("ANTHROPIC_ADMIN_KEY", "")
ADMIN_BASE = "https://api.anthropic.com"
CACHE      = {}
CACHE_TTL  = 300  # 5 minutes

KNOWN_NAMES = json.loads(os.environ.get("KEY_NAMES", "{}"))


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


def date_params(days):
    end   = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    return {
        "start_time": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "end_time":   end.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def fetch_cost_report(group_by, days=30):
    cache_key = f"cost_{group_by}_{days}"
    cached = _cache_get(cache_key)
    if cached:
        return cached

    params = {"group_by[]": group_by, **date_params(days)}
    resp = requests.get(
        f"{ADMIN_BASE}/v1/organizations/cost_report",
        headers=_admin_headers(),
        params=params,
        timeout=15,
    )
    if not resp.ok:
        raise ValueError(f"Admin API {resp.status_code}: {resp.text[:400]}")
    data = resp.json()
    _cache_set(cache_key, data)
    return data


def fetch_keys():
    cached = _cache_get("api_keys")
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
    _cache_set("api_keys", keys)
    return keys


def to_dollars(raw):
    """
    Convert raw API cost value to dollars.
    Anthropic returns costs in millicents (1/100000 of a dollar).
    Divisor is configurable via COST_DIVISOR env var for easy tuning.
    """
    divisor = float(os.environ.get("COST_DIVISOR", "100000"))
    return raw / divisor


def get_cost(entry):
    """Flexibly extract cost from an entry — handles different field names."""
    for field in ("total_cost", "cost", "amount", "total"):
        if field in entry:
            return entry[field]
    return 0


def get_rows(report):
    """Flexibly extract the rows array from a report response."""
    for field in ("data", "results", "items", "rows"):
        if field in report and isinstance(report[field], list):
            return report[field]
    return []


@app.route("/")
def index():
    return app.send_static_file("index.html")


@app.route("/api/health")
def health():
    return jsonify({
        "status": "ok",
        "credentials": "configured" if ADMIN_KEY else "missing",
    })


@app.route("/api/debug")
def debug():
    """
    Returns raw API responses — visit /api/debug in your browser to see
    the exact field names and values the Anthropic API is sending back.
    This helps diagnose unit/field-name issues.
    """
    if not ADMIN_KEY:
        return jsonify({"error": "ANTHROPIC_ADMIN_KEY not set"}), 500
    try:
        days = int(request.args.get("days", 7))
        key_raw   = fetch_cost_report("api_key_id",  days)
        model_raw = fetch_cost_report("description", days)
        keys_raw  = requests.get(
            f"{ADMIN_BASE}/v1/organizations/api_keys",
            headers=_admin_headers(), timeout=10,
        ).json()
        return jsonify({
            "note": "Raw API responses — use field names here to fix parsing",
            "cost_by_key_first_3_rows":   get_rows(key_raw)[:3],
            "cost_by_model_first_3_rows": get_rows(model_raw)[:3],
            "api_keys_first_3":           keys_raw.get("data", [])[:3],
            "full_key_report_keys":       list(key_raw.keys()),
            "days": days,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/usage")
def usage():
    try:
        days = int(request.args.get("days", 30))
        days = max(1, min(days, 90))

        if not ADMIN_KEY:
            return jsonify({"error": "ANTHROPIC_ADMIN_KEY not set"}), 500

        key_report   = fetch_cost_report("api_key_id",  days)
        model_report = fetch_cost_report("description", days)

        key_names = fetch_keys()
        key_names.update(KNOWN_NAMES)

        # --- Keys ---
        keys_data = []
        for entry in get_rows(key_report):
            key_id = entry.get("api_key_id") or entry.get("key_id") or entry.get("id") or "unknown"
            raw    = get_cost(entry)
            cost   = to_dollars(raw)
            if cost < 0.0001:
                continue
            keys_data.append({
                "id":            key_id,
                "name":          key_names.get(key_id, key_id[:16] + "…"),
                "cost":          round(cost, 2),
                "raw_cost":      raw,
                "input_tokens":  entry.get("input_tokens", 0),
                "output_tokens": entry.get("output_tokens", 0),
            })
        keys_data.sort(key=lambda x: x["cost"], reverse=True)

        # --- Models ---
        models_data = []
        for entry in get_rows(model_report):
            model = (entry.get("description") or entry.get("model") or
                     entry.get("model_id") or "unknown")
            raw   = get_cost(entry)
            cost  = to_dollars(raw)
            if cost < 0.0001:
                continue
            models_data.append({
                "model":         model,
                "cost":          round(cost, 2),
                "input_tokens":  entry.get("input_tokens", 0),
                "output_tokens": entry.get("output_tokens", 0),
            })
        models_data.sort(key=lambda x: x["cost"], reverse=True)

        total = round(sum(k["cost"] for k in keys_data), 2)

        return jsonify({
            "days":    days,
            "total":   total,
            "keys":    keys_data,
            "models":  models_data,
            "updated": datetime.now(timezone.utc).isoformat(),
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(debug=True, port=5001)
