"""Contoso Commerce — the first use case's warehouse shape and data.

Shape comes from `columns.json` (copied verbatim from
contoso-data-product-fabric-notebook-pipelines/gold/_columns.json, the T-SQL
columns dbt-fabric materialised). Rows are generated deterministically and
kept INTERNALLY CONSISTENT: fct_revenue_summary and fct_daily_revenue are
aggregations of fct_sales/fct_orders, so an eval's gold SQL against the
summary and the agent's SQL against the facts must agree.

Business semantics (fiscal year starts 1 April, segments, channel systems)
mirror contoso-data-product's gold schema.yml. They are restated here only to
GENERATE data; the agent learns them from OpenMetadata (seed/govern.py), never
from this module.
"""
from __future__ import annotations

import datetime as dt
import decimal
import json
import pathlib
import random

HERE = pathlib.Path(__file__).resolve().parent
COLUMNS = json.loads((HERE / "columns.json").read_text())

SCHEMA = "dbo"
WORKSPACE = "contoso-analytics"
WAREHOUSE = "contoso_warehouse"

# T-SQL types for the bare kinds in columns.json. varchar lengths are a seed
# choice; OpenMetadata requires dataLength for varchar, so every one is sized.
_VARCHAR = {
    "email": 200, "name": 120, "product_name": 120, "country_name": 80,
    "fiscal_year_label": 12, "fiscal_quarter_label": 12, "order_date": 10,
    "country": 2, "currency": 3,
}


def sql_type(col: dict) -> str:
    t = col["type"]
    if t == "varchar":
        return f"varchar({_VARCHAR.get(col['name'], 64)})"
    if t == "decimal":
        return "decimal(19,6)" if col["name"] == "rate_to_usd" else "decimal(19,4)"
    return t  # int, bigint, bit, date


def ddl(table: str) -> str:
    cols = ",\n  ".join(f"[{c['name']}] {sql_type(c)}" for c in COLUMNS[table])
    return f"CREATE TABLE [{SCHEMA}].[{table}] (\n  {cols}\n)"


# ------------------------------------------------------------- generation --
COUNTRIES = [("US", "United States"), ("GB", "United Kingdom"), ("SG", "Singapore")]
CURRENCY = {"US": "USD", "GB": "GBP", "SG": "SGD"}
RATE = {"USD": decimal.Decimal("1.000000"), "GBP": decimal.Decimal("1.270000"),
        "SGD": decimal.Decimal("0.740000")}
MARKETING = ["value", "mainstream", "premium", "lapsed", "new"]
LOYALTY = ["bronze", "silver", "gold"]
DEPARTMENTS = {"Electronics": ["Audio", "Computing", "Phones"],
               "Home": ["Kitchen", "Furniture"], "Sport": ["Outdoor", "Fitness"]}
PRODUCT_SEGMENT = ["Core", "Peripheral", "Unallocated"]
CHANNEL_SYSTEM = ["pos", "web"]
STATUS = ["settled", "settled", "settled", "cancelled", "pending"]

START, END = dt.date(2024, 4, 1), dt.date(2026, 3, 31)  # two full fiscal years


def fiscal(d: dt.date) -> tuple[int, int, int]:
    """(fiscal_year, fiscal_quarter, fiscal_period). FY starts 1 April and is
    named by the calendar year in which it ENDS (FY2025 = Apr 2024 .. Mar 2025)."""
    fy = d.year + 1 if d.month >= 4 else d.year
    period = (d.month - 4) % 12 + 1
    return fy, (period - 1) // 3 + 1, period


def money(x: float) -> decimal.Decimal:
    return decimal.Decimal(f"{x:.4f}")


def generate(n_customers=400, n_products=40, n_orders=6000, seed=20260822) -> dict[str, list[tuple]]:
    rnd = random.Random(seed)
    out: dict[str, list[tuple]] = {}

    out["dim_country"] = [(c, n) for c, n in COUNTRIES]

    dates = []
    d = START
    while d <= END:
        fy, fq, fp = fiscal(d)
        dates.append((d, d.year, d.month, (d.month - 1) // 3 + 1, fy, fq, fp,
                      f"FY{fy}", f"FY{fy}-Q{fq}"))
        d += dt.timedelta(days=1)
    out["dim_date"] = dates

    products = []
    for i in range(n_products):
        dept = rnd.choice(list(DEPARTMENTS))
        cat = rnd.choice(DEPARTMENTS[dept])
        products.append((f"P{i+1:04d}", f"{cat} item {i+1}", cat, dept,
                         rnd.choices(PRODUCT_SEGMENT, [6, 3, 1])[0],
                         money(rnd.uniform(8, 900))))
    out["dim_product"] = products

    customers = []
    for i in range(n_customers):
        country = rnd.choice(COUNTRIES)[0]
        customers.append((f"C{i+1:05d}", f"Customer {i+1}", f"customer{i+1}@example.com",
                          country, rnd.choices(MARKETING, [3, 4, 2, 1, 2])[0], rnd.choice(LOYALTY)))
    out["dim_customer"] = customers

    # Party = conformed customer across POS and web. Keep it 1:1 with customers
    # but mark the systems they appear in.
    parties = []
    for c in customers:
        in_pos, in_web = rnd.random() < 0.8, rnd.random() < 0.6
        if not (in_pos or in_web):
            in_pos = True
        parties.append((f"PK-{c[0]}", c[2], c[0] if in_pos else None, in_pos, in_web,
                        c[3], c[4], c[5]))
    out["dim_party"] = parties
    party_by_customer = {c[0]: p for c, p in zip(customers, parties)}

    orders, sales = [], []
    for i in range(n_orders):
        c = rnd.choice(customers)
        p = rnd.choice(products)
        od = START + dt.timedelta(days=rnd.randrange((END - START).days + 1))
        qty = rnd.choices([1, 2, 3, 5], [8, 4, 2, 1])[0]
        price = p[5] * decimal.Decimal(f"{rnd.uniform(0.8, 1.05):.4f}")
        price = price.quantize(decimal.Decimal("0.0001"))
        cur = CURRENCY[c[3]]
        rate = RATE[cur]
        carried = rnd.random() < 0.05
        amount = (price * qty).quantize(decimal.Decimal("0.0001"))
        amount_usd = (amount * rate).quantize(decimal.Decimal("0.0001"))
        status = rnd.choice(STATUS)
        party = party_by_customer[c[0]]
        channel = "pos" if (party[3] and (not party[4] or rnd.random() < 0.6)) else "web"
        orders.append((f"O{i+1:07d}", c[0], p[0], od.isoformat(), channel, status, cur, qty,
                       price, amount, rate, carried, amount_usd))
        sales.append((party[0], f"S{i+1:07d}", p[0], od.isoformat(), channel,
                      status == "cancelled", qty, amount, cur, rate, carried, amount_usd))
    out["fct_orders"] = orders
    out["fct_sales"] = sales

    # Aggregations — the warehouse's own "gold" facts, derived so they agree.
    prod_by_id = {p[0]: p for p in products}
    party_by_key = {p[0]: p for p in parties}
    summary: dict[tuple, list] = {}
    daily: dict[tuple, list] = {}
    for s in sales:
        party, _, pid, od, channel, cancelled, qty, _amt, _cur, rate, carried, amount_usd = s
        d = dt.date.fromisoformat(od)
        fy, fq, _ = fiscal(d)
        p = prod_by_id[pid]
        pt = party_by_key[party]
        key = (fy, f"FY{fy}", fq, f"FY{fy}-Q{fq}", channel, p[3], p[4], pt[6], pt[5])
        row = summary.setdefault(key, [0, 0, decimal.Decimal(0), decimal.Decimal(0), decimal.Decimal(0)])
        if cancelled:
            row[3] += amount_usd
        else:
            row[0] += 1
            row[1] += qty
            row[2] += amount_usd
            if carried:
                row[4] += amount_usd
        if not cancelled:
            dk = (od, pt[5])
            dr = daily.setdefault(dk, [0, 0, decimal.Decimal(0)])
            dr[0] += 1
            dr[1] += qty
            dr[2] += amount_usd
    out["fct_revenue_summary"] = [k + tuple(v) for k, v in sorted(summary.items())]
    out["fct_daily_revenue"] = [k + tuple(v) for k, v in sorted(daily.items())]
    return out


if __name__ == "__main__":
    data = generate()
    for t, rows in data.items():
        print(f"{t:22s} {len(rows):6d} rows   e.g. {rows[0]}")
