
import os
import re
import logging
from datetime import datetime, date
from zoneinfo import ZoneInfo

import requests
from flask import Flask, request, jsonify

# ============================================================
# Ledger Agent — IIT Madras TDS
# ============================================================

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

DATA_URL = os.getenv("DATA_URL", "").strip()
TIMEOUT = 60
LOCAL_TZ = ZoneInfo("Asia/Kolkata")

ORDERS = []
RATES = {}
PRODUCTS = set()
CUSTOMERS = set()


# ============================================================
# DATA LOADING
# ============================================================

def fetch_data(path):
    """Fetch data from the assignment API."""
    if not DATA_URL:
        raise RuntimeError("DATA_URL environment variable is missing.")

    response = requests.get(
        DATA_URL,
        params={"path": path},
        timeout=TIMEOUT
    )
    response.raise_for_status()
    return response


def load_ledger():
    """Load exchange rates and deduplicate orders by ID."""
    rates_response = fetch_data("/rates")
    rates_data = rates_response.json()

    if isinstance(rates_data, dict) and "rates" in rates_data:
        rates_data = rates_data["rates"]

    if not isinstance(rates_data, dict):
        raise ValueError("Unexpected exchange-rate response format.")

    rates = {}
    for currency, value in rates_data.items():
        if isinstance(value, dict):
            value = value.get("usd_per_unit", value.get("rate"))
        try:
            rates[str(currency).upper()] = float(value)
        except (TypeError, ValueError):
            continue

    # USD is the base currency.
    rates.setdefault("USD", 1.0)

    export_response = fetch_data("/export")
    text = export_response.text.strip()

    if not text:
        raise ValueError("The orders export is empty.")

    orders_by_id = {}

    # The export is expected to be newline-delimited JSON (NDJSON).
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue

        try:
            order = __import__("json").loads(line)
        except ValueError:
            # Also support a single JSON array/object response.
            try:
                parsed = export_response.json()
                if isinstance(parsed, list):
                    rows = parsed
                elif isinstance(parsed, dict):
                    rows = parsed.get("orders", parsed.get("data", []))
                else:
                    rows = []
                for row in rows:
                    if isinstance(row, dict):
                        add_order(orders_by_id, row)
                break
            except (ValueError, TypeError):
                logging.warning("Skipping an invalid export line.")
                continue

        if isinstance(order, dict):
            add_order(orders_by_id, order)

    orders = list(orders_by_id.values())

    products = {
        str(order.get("product", "")).strip()
        for order in orders
        if order.get("product")
    }
    customers = {
        str(order.get("customer", "")).strip()
        for order in orders
        if order.get("customer")
    }

    return orders, rates, products, customers


def parse_timestamp(value):
    """Parse a timestamp into a comparable datetime."""
    if not value:
        return datetime.min.replace(tzinfo=LOCAL_TZ)

    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=LOCAL_TZ)
        return dt
    except (ValueError, TypeError):
        return datetime.min.replace(tzinfo=LOCAL_TZ)


def add_order(orders_by_id, order):
    """Keep the most recently updated version of each order ID."""
    order_id = order.get("id")
    if order_id is None:
        return

    key = str(order_id)
    existing = orders_by_id.get(key)

    if existing is None:
        orders_by_id[key] = order
        return

    old_updated = parse_timestamp(existing.get("updated_at"))
    new_updated = parse_timestamp(order.get("updated_at"))

    if new_updated >= old_updated:
        orders_by_id[key] = order


# Load data when the service starts.
try:
    ORDERS, RATES, PRODUCTS, CUSTOMERS = load_ledger()
    logging.info("Loaded %s unique orders.", len(ORDERS))
except Exception:
    logging.exception("Unable to load ledger data.")
    ORDERS, RATES, PRODUCTS, CUSTOMERS = [], {"USD": 1.0}, set(), set()


# ============================================================
# HELPERS
# ============================================================

def normalize(text):
    return re.sub(r"\s+", " ", str(text).lower().strip())


def local_date(value):
    """Convert a timestamp to its Asia/Kolkata calendar date."""
    if not value:
        return None

    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=LOCAL_TZ)
        return dt.astimezone(LOCAL_TZ).date()
    except (ValueError, TypeError):
        return None


def amount_in_original_currency(order):
    """Return the order amount, using qty × unit_price as fallback."""
    amount = order.get("amount")

    try:
        if amount is not None and amount != "":
            return float(amount)
    except (TypeError, ValueError):
        pass

    try:
        return float(order.get("qty", 0)) * float(order.get("unit_price", 0))
    except (TypeError, ValueError):
        return 0.0


def usd(order):
    """Convert an order amount to USD."""
    currency = str(order.get("currency", "USD")).upper()
    rate = RATES.get(currency)

    if rate is None:
        logging.warning("Unknown currency %s; treating amount as zero.", currency)
        return 0.0

    return amount_in_original_currency(order) * float(rate)


def status_of(order):
    return str(order.get("status", "")).strip().lower()


def is_paid(order):
    return status_of(order) == "paid"


def is_refunded(order):
    return status_of(order) == "refunded"


def is_void(order):
    return status_of(order) == "void"


def unique_orders(orders):
    """Return unique order IDs."""
    result = {}
    for order in orders:
        if order.get("id") is not None:
            result[str(order["id"])] = order
    return list(result.values())


def month_range(question):
    """Extract a named month and year from the question."""
    q = normalize(question)

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

    for name in sorted(months, key=len, reverse=True):
        match = re.search(
            r"\b" + re.escape(name) + r"\b(?:\s+of)?(?:\s+(20\d{2}))?",
            q
        )
        if match:
            year = int(match.group(1)) if match.group(1) else 2026
            month = months[name]
            start = date(year, month, 1)

            if month == 12:
                end = date(year + 1, 1, 1)
            else:
                end = date(year, month + 1, 1)

            return start, end

    # Also support explicit ISO dates.
    dates = re.findall(r"\b(20\d{2}-\d{2}-\d{2})\b", q)
    if len(dates) >= 2:
        try:
            start = date.fromisoformat(dates[0])
            end = date.fromisoformat(dates[1])
            return start, date.fromordinal(end.toordinal() + 1)
        except ValueError:
            pass
    elif len(dates) == 1:
        try:
            start = date.fromisoformat(dates[0])
            return start, date.fromordinal(start.toordinal() + 1)
        except ValueError:
            pass

    return None, None


def extract_region(question):
    q = normalize(question)
    regions = ["north", "south", "east", "west", "central"]

    for region in regions:
        if re.search(r"\b" + region + r"\b", q):
            return region.title()

    return None


def extract_product(question):
    """Find a product name mentioned in the question."""
    q = normalize(question)

    for product in sorted(PRODUCTS, key=len, reverse=True):
        if product and re.search(
            r"(?<!\w)" + re.escape(product.lower()) + r"(?!\w)", q
        ):
            return product

    return None


def extract_customer(question):
    match = re.search(r"\bC\d+\b", str(question), re.IGNORECASE)
    return match.group(0).upper() if match else None


def filter_orders(question):
    """Apply date, region, product and customer filters."""
    selected = list(ORDERS)

    start, end = month_range(question)
    if start is not None:
        filtered = []
        for order in selected:
            d = local_date(order.get("created_at"))
            if d is not None and start <= d < end:
                filtered.append(order)
        selected = filtered

    region = extract_region(question)
    if region:
        selected = [
            order for order in selected
            if str(order.get("region", "")).strip().lower() == region.lower()
        ]

    product = extract_product(question)
    if product:
        selected = [
            order for order in selected
            if str(order.get("product", "")).strip().lower()
            == product.lower()
        ]

    customer = extract_customer(question)
    if customer:
        selected = [
            order for order in selected
            if str(order.get("customer", "")).strip().upper() == customer
        ]

    return unique_orders(selected)


def wants_count(question):
    q = normalize(question)

    patterns = [
        r"\bhow many\b",
        r"\bnumber of\b",
        r"\bcount of\b",
        r"\bcount orders\b",
        r"\bnumber of orders\b",
        r"\bhow many orders\b",
        r"\bhow many customers\b",
        r"\bhow many products\b",
        r"\bnumber of customers\b",
    ]
    return any(re.search(pattern, q) for pattern in patterns)


def asks_for_amount(question):
    q = normalize(question)
    return any(term in q for term in [
        "amount", "value", "revenue", "sales value",
        "sales revenue", "earned", "in usd", "dollar",
        "refund amount", "refund value", "total sales"
    ])


def asks_for_revenue(question):
    q = normalize(question)
    return any(term in q for term in [
        "revenue", "earned", "sales value",
        "sales revenue", "total sales"
    ])


def asks_for_average_order_value(question):
    q = normalize(question)
    return (
        "average" in q
        and (
            "order value" in q
            or "value of orders" in q
            or "order amount" in q
            or "order size" in q
        )
    )


def is_product_ranking_question(question):
    q = normalize(question)

    ranking_words = [
        "top-selling", "top selling", "best-selling", "best selling",
        "most sold", "top product", "best product",
        "highest revenue", "most revenue", "earned the most",
        "highest-selling", "highest selling"
    ]

    if any(word in q for word in ranking_words):
        return True

    return (
        ("which product" in q or "what product" in q)
        and any(word in q for word in [
            "most", "highest", "top", "best", "revenue", "earned"
        ])
    )


def asks_for_units_sold(question):
    q = normalize(question)
    return any(phrase in q for phrase in [
        "units sold", "quantity sold", "total quantity",
        "how many units", "number of units", "items sold"
    ])


def asks_for_order_count(question):
    q = normalize(question)
    return wants_count(question) and (
        "order" in q or "orders" in q
    )


# ============================================================
# QUESTION ANSWERING
# ============================================================

def answer_question(question):
    q = normalize(question)
    selected = filter_orders(question)

    if not selected:
        return 0 if (
            wants_count(question)
            or "revenue" in q
            or "refund" in q
            or "sales" in q
        ) else None

    # 1. Refund questions: count orders when asked "how many";
    # otherwise return the refunded amount in USD.
    if "refund" in q or "refunded" in q:
        refunded = [order for order in selected if is_refunded(order)]

        if wants_count(question) and not asks_for_amount(question):
            return len(unique_orders(refunded))

        return round(sum(usd(order) for order in refunded), 2)

    # 2. Product ranking must be checked before general revenue.
    if is_product_ranking_question(question):
        paid = [order for order in selected if is_paid(order)]
        if not paid:
            return None

        totals = {}
        for order in paid:
            product = str(order.get("product", "Unknown"))
            if asks_for_revenue(question):
                totals[product] = totals.get(product, 0.0) + usd(order)
            else:
                try:
                    quantity = float(order.get("qty", 0))
                except (TypeError, ValueError):
                    quantity = 0.0
                totals[product] = totals.get(product, 0.0) + quantity

        if not totals:
            return None

        # Deterministic tie-breaker: alphabetical product name.
        best_product = sorted(
            totals.items(),
            key=lambda item: (-item[1], item[0].lower())
        )[0][0]
        return best_product

    # 3. Average paid order value in USD.
    if asks_for_average_order_value(question):
        paid = [order for order in selected if is_paid(order)]
        if not paid:
            return None
        return round(sum(usd(order) for order in paid) / len(paid), 2)

    # 4. Distinct customers who placed a paid order.
    if (
        "customer" in q
        and wants_count(question)
    ):
        paid = [order for order in selected if is_paid(order)]
        return len({
            str(order.get("customer")).strip()
            for order in paid
            if order.get("customer") is not None
        })

    # 5. Number of different products.
    if (
        "product" in q
        and wants_count(question)
        and not is_product_ranking_question(question)
    ):
        paid = [order for order in selected if is_paid(order)]
        return len({
            str(order.get("product")).strip()
            for order in paid
            if order.get("product")
        })

    # 6. Number of orders. When the question specifies paid/refunded/void,
    # apply that status; otherwise count distinct matching order IDs.
    if asks_for_order_count(question):
        if "paid" in q:
            selected = [order for order in selected if is_paid(order)]
        elif "void" in q or "cancelled" in q or "canceled" in q:
            selected = [order for order in selected if is_void(order)]
        return len(unique_orders(selected))

    # 7. Total units sold. Only paid orders count as sales.
    if asks_for_units_sold(question):
        paid = [order for order in selected if is_paid(order)]
        total = 0.0
        for order in paid:
            try:
                total += float(order.get("qty", 0))
            except (TypeError, ValueError):
                continue
        return int(total) if total.is_integer() else round(total, 2)

    # 8. General revenue / sales value: paid orders only.
    if asks_for_revenue(question) or "revenue" in q:
        paid = [order for order in selected if is_paid(order)]
        return round(sum(usd(order) for order in paid), 2)

    # 9. Generic count of orders.
    if wants_count(question):
        return len(unique_orders(selected))

    # 10. Fallback: return paid revenue only when the question clearly asks
    # about sales, otherwise avoid inventing an answer.
    if "sales" in q or "turnover" in q:
        paid = [order for order in selected if is_paid(order)]
        return round(sum(usd(order) for order in paid), 2)

    return None


# ============================================================
# HTTP ENDPOINT
# ============================================================

@app.route("/", methods=["GET"])
def health():
    return jsonify({
        "service": "Ledger Agent",
        "status": "ready" if ORDERS else "data_not_loaded",
        "orders_loaded": len(ORDERS)
    })


@app.route("/", methods=["POST"])
def ask():
    payload = request.get_json(silent=True)

    if not isinstance(payload, dict):
        return jsonify({"answer": None, "error": "Send a JSON object."}), 400

    question = payload.get("question")

    if not isinstance(question, str) or not question.strip():
        return jsonify({
            "answer": None,
            "error": "The 'question' field is required."
        }), 400

    if not ORDERS:
        return jsonify({
            "answer": None,
            "error": "Ledger data is not loaded. Check DATA_URL."
        }), 503

    try:
        answer = answer_question(question)
        return jsonify({"answer": answer})
    except Exception:
        logging.exception("Question processing failed.")
        return jsonify({"answer": None}), 500


if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
