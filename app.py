
import os
import re
import json
import requests
from datetime import datetime
from zoneinfo import ZoneInfo
from flask import Flask, request, jsonify

app = Flask(__name__)

# Paste your complete working API URL from Colab here.
DATA_URL = os.environ.get("DATA_URL", "PASTE_YOUR_FULL_API_URL_HERE")

TZ = ZoneInfo("Asia/Kolkata")
SESSION = requests.Session()

def fetch_data(path):
    """Fetch one API endpoint using the assignment credentials."""
    from urllib.parse import urlparse, parse_qs

    parsed = urlparse(DATA_URL)
    params = {
        k: v[0] for k, v in parse_qs(parsed.query).items()
    }
    params["path"] = path

    response = SESSION.get(
        f"{parsed.scheme}://{parsed.netloc}{parsed.path}",
        params=params,
        timeout=10,
    )
    response.raise_for_status()
    return response

def load_ledger():
    # The export endpoint returns newline-delimited JSON.
    response = fetch_data("/export")
    rows = [
        json.loads(line)
        for line in response.text.splitlines()
        if line.strip()
    ]

    # Keep the newest version of each order ID.
    latest = {}
    for row in rows:
        order_id = row["id"]
        if (
            order_id not in latest
            or row["updated_at"] > latest[order_id]["updated_at"]
        ):
            latest[order_id] = row

    rates_data = fetch_data("/rates").json()
    rates = rates_data["usd_per_unit"]

    return list(latest.values()), rates

ORDERS, RATES = load_ledger()

def local_date(value):
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt.astimezone(TZ).date()

def usd(order):
    return float(order["amount"]) * RATES[order["currency"]]

def month_range(question):
    """Return inclusive start and exclusive end dates if a month is mentioned."""
    q = question.lower()
    months = {
        "january": 1, "jan": 1,
        "february": 2, "feb": 2,
        "march": 3, "mar": 3,
        "april": 4, "apr": 4,
        "may": 5,
        "june": 6, "jun": 6,
        "july": 7, "jul": 7,
        "august": 8, "aug": 8,
        "september": 9, "sep": 9, "sept": 9,
        "october": 10, "oct": 10,
        "november": 11, "nov": 11,
        "december": 12, "dec": 12,
    }

    for name, month in months.items():
        match = re.search(
            rf"\b{name}\b(?:\s+(?:of\s+)?)?(20\d{{2}})?",
            q
        )
        if match:
            year = int(match.group(1)) if match.group(1) else 2026
            start = datetime(year, month, 1).date()
            if month == 12:
                end = datetime(year + 1, 1, 1).date()
            else:
                end = datetime(year, month + 1, 1).date()
            return start, end

    return None, None

def extract_region(question):
    q = question.lower()
    for region in ["North", "South", "East", "West", "Central"]:
        if re.search(rf"\b{region.lower()}\b", q):
            return region
    return None

def extract_product(question):
    q = question.lower()
    products = sorted(
        {o["product"] for o in ORDERS},
        key=len,
        reverse=True
    )
    for product in products:
        if product.lower() in q:
            return product
    return None

def extract_customer(question):
    match = re.search(r"\bC\d{4}\b", question, re.IGNORECASE)
    return match.group(0).upper() if match else None

def answer_question(question):
    q = question.lower().strip()

    start, end = month_range(q)
    region = extract_region(question)
    product = extract_product(question)
    customer = extract_customer(question)

    # Use created_at for the period of an order.
    selected = ORDERS[:]

    if start:
        selected = [
            o for o in selected
            if start <= local_date(o["created_at"]) < end
        ]

    if region:
        selected = [o for o in selected if o["region"] == region]

    if product:
        selected = [o for o in selected if o["product"] == product]

    if customer:
        selected = [o for o in selected if o["customer"] == customer]

    # Revenue counts only paid orders.
    if any(word in q for word in ["revenue", "sales", "sold", "turnover"]):
        paid = [o for o in selected if o["status"] == "paid"]
        return round(sum(usd(o) for o in paid), 2)

    # Refund amounts are reported separately from revenue.
    if any(word in q for word in ["refund", "refunded"]):
        refunded = [o for o in selected if o["status"] == "refunded"]
        return round(sum(usd(o) for o in refunded), 2)

    # Order counts.
    if any(word in q for word in ["how many orders", "number of orders", "count"]):
        return len(selected)

    # Quantity totals.
    if any(word in q for word in ["how many units", "units sold", "quantity", "qty"]):
        relevant = [o for o in selected if o["status"] == "paid"]
        return sum(o["qty"] for o in relevant)

    # Customer questions.
    if "customer" in q and any(
        word in q for word in ["unique", "distinct", "how many"]
    ):
        return len({o["customer"] for o in selected})

    # Product questions.
    if "product" in q and any(
        word in q for word in ["unique", "distinct", "how many"]
    ):
        return len({o["product"] for o in selected})

    # Return the most common product for simple "best-selling" questions.
    if any(word in q for word in ["best-selling", "bestselling", "most sold"]):
        totals = {}
        for o in selected:
            if o["status"] == "paid":
                totals[o["product"]] = (
                    totals.get(o["product"], 0) + o["qty"]
                )
        return max(totals, key=totals.get) if totals else None

    return None

@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "service": "Ledger Agent",
        "status": "ready",
        "orders_loaded": len(ORDERS)
    })

@app.route("/", methods=["POST"])
def handle_question():
    body = request.get_json(silent=True) or {}
    question = body.get("question")

    if not isinstance(question, str) or not question.strip():
        return jsonify({"answer": None}), 400

    try:
        answer = answer_question(question)
        return jsonify({"answer": answer})
    except Exception as exc:
        app.logger.exception("Question processing failed")
        return jsonify({"answer": None, "error": str(exc)}), 500

if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 10000))
    )
