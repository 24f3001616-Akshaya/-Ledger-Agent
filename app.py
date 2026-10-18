
import os
import re
import json
import logging
import requests

from datetime import datetime
from zoneinfo import ZoneInfo
from urllib.parse import urlparse, parse_qs

from flask import Flask, request, jsonify

app = Flask(__name__)
app.logger.setLevel(logging.INFO)

TZ = ZoneInfo("Asia/Kolkata")
SESSION = requests.Session()

DATA_URL = os.environ.get("DATA_URL", "").strip()

ORDERS = []
RATES = {}


# --------------------------------------------------
# 1. LOAD ASSIGNMENT DATA
# --------------------------------------------------

def fetch_data(path):
    if not DATA_URL or "PASTE_YOUR_FULL_API_URL_HERE" in DATA_URL:
        raise RuntimeError("DATA_URL environment variable is missing.")

    parsed = urlparse(DATA_URL)

    if parsed.scheme != "https" or not parsed.netloc:
        raise RuntimeError("DATA_URL must be a valid HTTPS URL.")

    params = {
        key: values[0]
        for key, values in parse_qs(parsed.query).items()
    }
    params["path"] = path

    response = SESSION.get(
        f"{parsed.scheme}://{parsed.netloc}{parsed.path}",
        params=params,
        timeout=30,
    )
    response.raise_for_status()
    return response


def load_ledger():
    response = fetch_data("/export")

    rows = []
    for line in response.text.splitlines():
        if line.strip():
            rows.append(json.loads(line))

    # Keep the latest version of each order ID.
    latest = {}

    for order in rows:
        order_id = order.get("id")
        updated_at = order.get("updated_at", "")

        if order_id is None:
            continue

        current = latest.get(order_id)

        if current is None or updated_at > current.get("updated_at", ""):
            latest[order_id] = order

    rates_data = fetch_data("/rates").json()
    rates = rates_data["usd_per_unit"]

    return list(latest.values()), rates


# Load data at startup. A failed load should be visible in Render logs.
try:
    ORDERS, RATES = load_ledger()
    app.logger.info("Loaded %s unique orders.", len(ORDERS))
except Exception:
    app.logger.exception("Could not load the assignment ledger.")
    ORDERS, RATES = [], {}


# --------------------------------------------------
# 2. GENERAL HELPERS
# --------------------------------------------------

def local_date(value):
    """Convert a timestamp to its Asia/Kolkata calendar date."""
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)

    return dt.astimezone(TZ).date()


def usd(order):
    """Convert the order's amount into USD."""
    currency = order.get("currency")
    if currency not in RATES:
        raise ValueError(f"Missing exchange rate for {currency}")

    return float(order["amount"]) * float(RATES[currency])


def month_range(question):
    """Return the start and end dates for a mentioned month."""
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

    for name, month in sorted(
        months.items(), key=lambda item: len(item[0]), reverse=True
    ):
        match = re.search(
            rf"\b{name}\b(?:\s+(?:of\s+)?)?(20\d{{2}})?",
            q,
        )

        if match:
            year = int(match.group(1)) if match.group(1) else 2026
            start = datetime(year, month, 1).date()

            if month == 12:
                end = datetime(year + 1, 1, 1).date()
            else:
                end = datetime(year, month + 1, 1).date()

            return start, end

    # Support explicit date ranges such as 2026-01-01 to 2026-02-01.
    match = re.search(
        r"\b(20\d{2}-\d{2}-\d{2})\s+(?:to|through|until)\s+"
        r"(20\d{2}-\d{2}-\d{2})\b",
        q,
    )

    if match:
        return (
            datetime.fromisoformat(match.group(1)).date(),
            datetime.fromisoformat(match.group(2)).date(),
        )

    return None, None


def extract_region(question):
    for region in ["North", "South", "East", "West", "Central"]:
        if re.search(rf"\b{region}\b", question, re.IGNORECASE):
            return region
    return None


def extract_product(question):
    q = question.lower()

    products = sorted(
        {str(order.get("product", "")) for order in ORDERS},
        key=len,
        reverse=True,
    )

    for product in products:
        if product and re.search(
            rf"(?<!\w){re.escape(product.lower())}(?!\w)", q
        ):
            return product

    return None


def extract_customer(question):
    match = re.search(r"\bC\d{4}\b", question, re.IGNORECASE)
    return match.group(0).upper() if match else None


def filter_orders(question):
    """Apply all recognized date, region, product and customer filters."""
    start, end = month_range(question)
    region = extract_region(question)
    product = extract_product(question)
    customer = extract_customer(question)

    selected = ORDERS[:]

    if start is not None:
        selected = [
            order for order in selected
            if order.get("created_at")
            and start <= local_date(order["created_at"]) < end
        ]

    if region:
        selected = [
            order for order in selected
            if str(order.get("region", "")).lower() == region.lower()
        ]

    if product:
        selected = [
            order for order in selected
            if str(order.get("product", "")).lower() == product.lower()
        ]

    if customer:
        selected = [
            order for order in selected
            if str(order.get("customer", "")).upper() == customer
        ]

    return selected


def paid_orders(orders):
    return [
        order for order in orders
        if str(order.get("status", "")).lower() == "paid"
    ]


# --------------------------------------------------
# 3. QUESTION CLASSIFICATION AND ANSWERS
# --------------------------------------------------

def answer_question(question):
    if not isinstance(question, str) or not question.strip():
        return None

    q = question.lower().strip()
    selected = filter_orders(question)

    if not ORDERS or not RATES:
        app.logger.error("Ledger is not loaded.")
        return None

    # A. Product earning the most USD revenue.
    # This MUST run before the general revenue calculation.
    asks_product_ranking = (
        "product" in q
        and any(
            phrase in q
            for phrase in [
                "most", "highest", "top", "best-selling",
                "bestselling", "top-selling", "top selling",
                "most sold", "earned the most", "highest revenue",
            ]
        )
    )

    asks_revenue_ranking = any(
        phrase in q
        for phrase in [
            "revenue", "earned", "sales value", "sales revenue",
            "in usd", "by revenue",
        ]
    )

    if asks_product_ranking:
        orders = paid_orders(selected)
        totals = {}

        for order in orders:
            product = order.get("product")
            if not product:
                continue

            if asks_revenue_ranking:
                value = usd(order)
            else:
                value = int(order.get("qty", 0))

            totals[product] = totals.get(product, 0) + value

        if not totals:
            return None

        # Return the product name, never the numeric total.
        return max(totals, key=totals.get)

    # B. Average / mean paid order value in USD.
    if any(word in q for word in ["average", "avg", "mean"]):
        if "order" in q or "order value" in q:
            orders = paid_orders(selected)

            if not orders:
                return None

            return round(
                sum(usd(order) for order in orders) / len(orders),
                2,
            )

    # C. Distinct customer count.
    if (
        "customer" in q
        and any(
            phrase in q
            for phrase in [
                "unique", "distinct", "different", "how many",
                "number of",
            ]
        )
    ):
        orders = selected

        if any(word in q for word in ["paid", "placed"]):
            orders = paid_orders(orders)

        return len({
            order.get("customer")
            for order in orders
            if order.get("customer") is not None
        })

    # D. Distinct product count.
    if (
        "product" in q
        and any(
            phrase in q
            for phrase in [
                "unique", "distinct", "different", "how many",
                "number of",
            ]
        )
    ):
        orders = selected

        if "paid" in q:
            orders = paid_orders(orders)

        return len({
            order.get("product")
            for order in orders
            if order.get("product") is not None
        })

    # E. Refund total. Check before general revenue.
    if "refund" in q or "refunded" in q:
        orders = [
            order for order in selected
            if str(order.get("status", "")).lower() == "refunded"
        ]

        return round(sum(usd(order) for order in orders), 2)

    # F. Total revenue / sales / turnover.
    # Only paid orders contribute to revenue.
    if any(
        word in q
        for word in ["revenue", "sales", "turnover", "total sold"]
    ):
        orders = paid_orders(selected)
        return round(sum(usd(order) for order in orders), 2)

    # G. Number of units sold.
    if any(
        phrase in q
        for phrase in [
            "units sold", "how many units", "quantity",
            "qty", "number of units",
        ]
    ):
        orders = paid_orders(selected)
        return sum(int(order.get("qty", 0)) for order in orders)

    # H. Number of orders.
    if any(
        phrase in q
        for phrase in [
            "how many orders", "number of orders",
            "count orders", "order count", "orders were placed",
            "how many were ordered",
        ]
    ):
        return len({
            order.get("id")
            for order in selected
            if order.get("id") is not None
        })

    # Unknown question: don't invent an answer.
    app.logger.warning("Question was not recognized: %s", question)
    return None


# --------------------------------------------------
# 4. HTTP ENDPOINTS
# --------------------------------------------------

@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "service": "Ledger Agent",
        "status": "ready" if ORDERS and RATES else "data_error",
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
        app.logger.exception("Question processing failed.")
        return jsonify({"answer": None}), 500


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 10000)),
    )
