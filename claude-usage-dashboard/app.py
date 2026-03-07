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
        "x-api-key": ADMIN_KEY,
        "anthropic-version": "2023-06-01",
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
    Probes multiple possible Anthropic Admin API endpoints and returns
    raw responses so we can find the right URL and field names.
    Visit /api/debug in your browser.
    """
    if not ADMIN_KEY:
        return jsonify({"error": "ANTHROPIC_ADMIN_KEY not set"}), 500

    results = {}
    days = int(request.args.get("days", 7))
    p = date_params(days)

    # Try every plausible endpoint path
    candidates = [
        ("cost_report_by_key",   "/v1/organizations/cost_report",  {"group_by[]": "api_key_id",  **p}),
        ("cost_report_by_model", "/v1/organizations/cost_report",  {"group_by[]": "description", **p}),
        ("usage_by_key",         "/v1/organizations/usage",        {"group_by[]": "api_key_id",  **p}),
        ("usage_by_model",       "/v1/organizations/usage",        {"group_by[]": "model",       **p}),
        ("spend",                "/v1/organizations/spend",        {**p}),
        ("api_keys",             "/v1/organizations/api_keys",     {}),
    ]

    for label, path, params in candidates:
        try:
            r = requests.get(
                f"{ADMIN_BASE}{path}",
                headers=_admin_headers(),
                params=params,
                timeout=10,
            )
            if r.ok:
                body = r.json()
                rows = get_rows(body)
                results[label] = {
                    "status":     r.status_code,
                    "url":        path,
                    "top_keys":   list(body.keys()),
                    "row_count":  len(rows),
                    "first_row":  rows[0] if rows else None,
                }
            else:
                results[label] = {
                    "status": r.status_code,
                    "url":    path,
                    "error":  r.text[:200],
                }
        except Exception as e:
            results[label] = {"url": path, "error": str(e)}

    return jsonify({"days_queried": days, "endpoints": results})


@app.route("/api/probe")
def probe():
    """Quick check — just tests the admin key is valid."""
    if not ADMIN_KEY:
        return jsonify({"error": "ANTHROPIC_ADMIN_KEY not set"}), 500
    r = requests.get(
        f"{ADMIN_BASE}/v1/organizations/api_keys",
        headers=_admin_headers(), timeout=10,
    )
    return jsonify({"status": r.status_code, "ok": r.ok, "body": r.json() if r.ok else r.text[:300]})


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
