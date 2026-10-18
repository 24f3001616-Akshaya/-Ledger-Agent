
import os
import re
import json
import requests
from datetime import datetime
from zoneinfo import ZoneInfo
from urllib.parse import urlparse, parse_qs
from flask import Flask, request, jsonify

app = Flask(__name__)

DATA_URL = os.environ.get("DATA_URL", "").strip()
TZ = ZoneInfo("Asia/Kolkata")
SESSION = requests.Session()


def fetch_data(path):
    """Fetch data from the assignment API."""
    if not DATA_URL or "PASTE_YOUR_FULL_API_URL_HERE" in DATA_URL:
        raise RuntimeError("DATA_URL is not configured correctly.")

    parsed = urlparse(DATA_URL)
    params = {
        key: values[0]
        for key, values in parse_qs(parsed.query).items()
    }
    params["path"] = path

    response = SESSION.get(
        f"{parsed.scheme}://{parsed.netloc}{parsed.path}",
        params=params,
        timeout=12,
    )
    response.raise_for_status()
    return response


def load_ledger():
    """Load all orders and keep the latest version of each order ID."""
    response = fetch_data("/export")
    rows = [
        json.loads(line)
        for line in response.text.splitlines()
        if line.strip()
    ]

    latest = {}
    for order in rows:
        order_id = order["id"]
        existing = latest.get(order_id)

        if (
            existing is None
            or order["updated_at"] > existing["updated_at"]
        ):
            latest[order_id] = order

    rates_data = fetch_data("/rates").json()
    rates = rates_data["usd_per_unit"]

    return list(latest.values()), rates


ORDERS, RATES = load_ledger()


def local_date(value):
    """Convert a timestamp into its Asia/Kolkata calendar date."""
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)

    return dt.astimezone(TZ).date()


def usd(order):
    """Convert an order amount to USD."""
    return float(order["amount"]) * float(RATES[order["currency"]])


def month_range(question):
    """Find a month and optional year in the question."""
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
        "september": 9, "sept": 9, "sep": 9,
        "october": 10, "oct": 10,
        "november": 11, "nov": 11,
        "december": 12, "dec": 12,
    }

    for name, month in months.items():
        pattern = rf"\b{name}\b(?:\s+(?:of\s+)?)?(20\d{{2}})?"
        match = re.search(pattern, q)

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
    for region in ["North", "South", "East", "West", "Central"]:
        if re.search(rf"\b{region}\b", question, re.IGNORECASE):
            return region
    return None


def extract_product(question):
    q = question.lower()
    products = sorted(
        {order["product"] for order in ORDERS},
        key=len,
        reverse=True,
    )

    for product in products:
        if product.lower() in q:
            return product
    return None


def extract_customer(question):
    match = re.search(r"\bC\d{4}\b", question, re.IGNORECASE)
    return match.group(0).upper() if match else None


def filter_orders(question):
    """Apply date, region, product, and customer filters."""
    start, end = month_range(question)
    region = extract_region(question)
    product = extract_product(question)
    customer = extract_customer(question)

    selected = ORDERS[:]

    if start is not None:
        selected = [
            order for order in selected
            if start <= local_date(order["created_at"]) < end
        ]

    if region:
        selected = [
            order for order in selected
            if order["region"].lower() == region.lower()
        ]

    if product:
        selected = [
            order for order in selected
            if order["product"].lower() == product.lower()
        ]

    if customer:
        selected = [
            order for order in selected
            if order["customer"].upper() == customer
        ]

    return selected


def answer_question(question):
    q = question.lower().strip()
    selected = filter_orders(question)

    # Average paid order value in USD.
    # Apply all filters first, then average paid orders only.
    if any(word in q for word in ["average", "avg", "mean"]):
        if "order" in q or "order value" in q:
            paid_orders = [
                order for order in selected
                if order["status"] == "paid"
            ]

            if not paid_orders:
                return None

            total_usd = sum(usd(order) for order in paid_orders)
            return round(total_usd / len(paid_orders), 2)

    # Distinct customers.
    if (
        "customer" in q
        and any(
            word in q
            for word in ["unique", "distinct", "different", "how many"]
        )
    ):
        relevant = selected

        if "paid" in q or "placed" in q:
            relevant = [
                order for order in relevant
                if order["status"] == "paid"
            ]

        return len({order["customer"] for order in relevant})

    # Distinct products.
    if (
        "product" in q
        and any(
            word in q
            for word in ["unique", "distinct", "different", "how many"]
        )
    ):
        relevant = selected

        if "paid" in q:
            relevant = [
                order for order in relevant
                if order["status"] == "paid"
            ]

        return len({order["product"] for order in relevant})

    # Top-selling product: revenue or units sold.
    if any(
        word in q
        for word in [
            "best-selling", "bestselling", "most sold",
            "top-selling", "top selling", "highest-selling",
            "top selling product",
        ]
    ):
        totals = {}
        paid_orders = [
            order for order in selected
            if order["status"] == "paid"
        ]

        if any(word in q for word in ["revenue", "sales value"]):
            for order in paid_orders:
                product = order["product"]
                totals[product] = totals.get(product, 0.0) + usd(order)
        else:
            for order in paid_orders:
                product = order["product"]
                totals[product] = totals.get(product, 0) + int(order["qty"])

        return max(totals, key=totals.get) if totals else None

    # Revenue: paid orders only.
    if any(
        word in q
        for word in ["revenue", "sales", "turnover", "total sold"]
    ):
        paid = [
            order for order in selected
            if order["status"] == "paid"
        ]
        return round(sum(usd(order) for order in paid), 2)

    # Refund totals.
    if "refund" in q or "refunded" in q:
        refunded = [
            order for order in selected
            if order["status"] == "refunded"
        ]
        return round(sum(usd(order) for order in refunded), 2)

    # Number of units sold.
    if any(
        word in q
        for word in ["units sold", "how many units", "quantity", "qty"]
    ):
        paid = [
            order for order in selected
            if order["status"] == "paid"
        ]
        return sum(int(order["qty"]) for order in paid)

    # Number of orders.
    if any(
        phrase in q
        for phrase in [
            "how many orders",
            "number of orders",
            "count orders",
            "order count",
            "orders were placed",
        ]
    ):
        return len({order["id"] for order in selected})

    return None


@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "service": "Ledger Agent",
        "status": "ready",
        "orders_loaded": len(ORDERS),
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
    except Exception:
        app.logger.exception("Question processing failed")
        return jsonify({"answer": None}), 500


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 10000)),
    )
