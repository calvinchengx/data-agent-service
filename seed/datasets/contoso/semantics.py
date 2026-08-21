"""Contoso Commerce — the semantic layer, as DATA for seed/govern.py.

Text is taken from contoso-data-product's gold models (schema.yml and the
model SQL headers): fiscal calendar, net-vs-cancelled revenue, segments,
channel systems. This is the knowledge the agent must retrieve from
OpenMetadata; it is never compiled into a prompt.
"""

SERVICE = "fabric_contoso"           # OM databaseService name == DAS_SOURCES[].om_service_fqn
DOMAIN = {"name": "contoso-commerce", "displayName": "Contoso Commerce",
          "domainType": "Consumer-aligned",
          "description": "Contoso's retail commerce across the POS and web selling systems."}
DATA_PRODUCT = {"name": "contoso-analytics", "displayName": "Contoso Analytics",
                "description": "Management reporting pack over Contoso's gold warehouse."}

GLOSSARY = {"name": "Contoso Commerce",
            "description": "Business vocabulary for Contoso's commerce reporting."}

# name -> description, synonyms, columns it governs (table.column)
TERMS = {
    "Fiscal Year": {
        "description": (
            "Contoso's financial year starts on **1 April** and is named by the calendar "
            "year in which it ENDS: FY2025 runs 1 April 2024 – 31 March 2025. July trading "
            "is therefore Q2, not Q3. Never substitute the calendar year."),
        "synonyms": ["FY", "financial year"],
        "columns": ["dim_date.fiscal_year", "dim_date.fiscal_year_label",
                    "fct_revenue_summary.fiscal_year", "fct_revenue_summary.fiscal_year_label"],
    },
    "Fiscal Quarter": {
        "description": "Quarter of the Fiscal Year: Q1 = Apr–Jun, Q2 = Jul–Sep, Q3 = Oct–Dec, Q4 = Jan–Mar.",
        "synonyms": ["FQ"],
        "columns": ["dim_date.fiscal_quarter", "dim_date.fiscal_quarter_label",
                    "fct_revenue_summary.fiscal_quarter", "fct_revenue_summary.fiscal_quarter_label"],
    },
    "Net Revenue": {
        "description": (
            "The headline revenue figure: USD value of sale lines that were NOT cancelled. "
            "Stored as `revenue_usd` in fct_revenue_summary, or computed from fct_sales as "
            "SUM(amount_usd) WHERE is_cancelled = 0. Gross would overstate the business by "
            "the cancellation rate (~5% of web orders); always report net unless asked for gross."),
        "synonyms": ["revenue", "net sales", "revenue_usd"],
        "columns": ["fct_revenue_summary.revenue_usd", "fct_sales.amount_usd"],
    },
    "Cancelled Revenue": {
        "description": "USD value of cancelled sale lines, reported alongside Net Revenue. Only the web system cancels; POS has no such concept.",
        "synonyms": ["cancellations", "cancelled_revenue_usd"],
        "columns": ["fct_revenue_summary.cancelled_revenue_usd", "fct_sales.is_cancelled"],
    },
    "Gross Revenue": {
        "description": "Net Revenue plus Cancelled Revenue: every sale line's USD value regardless of cancellation.",
        "synonyms": ["gross sales"],
        "columns": [],
    },
    "Customer Segment": {
        "description": (
            "Marketing segment assigned by the POS system: value, mainstream, premium, lapsed, new. "
            "Web-only shoppers have no segment and are reported as **Unsegmented** in "
            "fct_revenue_summary (NULL in dim_party)."),
        "synonyms": ["marketing segment", "segment"],
        "columns": ["dim_customer.marketing_segment", "dim_party.marketing_segment",
                    "fct_revenue_summary.customer_segment"],
    },
    "Product Segment": {
        "description": "Group data office rollup of products: Core, Peripheral, or Unallocated (SKUs the web store sells that were never published to the hierarchy).",
        "synonyms": [],
        "columns": ["dim_product.product_segment", "fct_revenue_summary.product_segment"],
    },
    "Channel System": {
        "description": "Which selling system booked the sale: POS (stores) or WEB (online storefront). 'Revenue is up' and 'online revenue is up' are different sentences.",
        "synonyms": ["channel", "selling system"],
        "columns": ["fct_sales.channel_system", "fct_revenue_summary.channel_system", "fct_orders.channel"],
    },
    "Carried FX Rate": {
        "description": "FX rates are published on trading days only; a sale on a non-trading day is converted at the last published rate (rate_is_carried = 1). revenue_at_carried_rate reports how much Net Revenue depends on a carried rate.",
        "synonyms": ["carried-forward rate"],
        "columns": ["fct_sales.rate_is_carried", "fct_orders.rate_is_carried",
                    "fct_revenue_summary.revenue_at_carried_rate"],
    },
    "Party": {
        "description": "A customer resolved ACROSS selling systems by email: one party may appear in POS, web, or both (in_pos / in_web). Use dim_party, not dim_customer, when counting people across channels.",
        "synonyms": ["resolved customer"],
        "columns": ["dim_party.party_key", "fct_sales.party_key"],
    },
}

# OpenMetadata Metric entities: the canonical formulas.
METRICS = [
    {"name": "net_revenue_usd", "displayName": "Net Revenue (USD)",
     "description": "Headline revenue: non-cancelled sale lines in USD. Prefer fct_revenue_summary.revenue_usd; equivalently SUM(amount_usd) over fct_sales WHERE is_cancelled = 0.",
     "metricType": "SUM", "unitOfMeasurement": "DOLLARS", "granularity": "DAY",
     "expression": "SELECT SUM(revenue_usd) FROM dbo.fct_revenue_summary",
     "terms": ["Net Revenue"]},
    {"name": "cancelled_revenue_usd", "displayName": "Cancelled Revenue (USD)",
     "description": "USD value of cancelled sale lines.",
     "metricType": "SUM", "unitOfMeasurement": "DOLLARS", "granularity": "DAY",
     "expression": "SELECT SUM(cancelled_revenue_usd) FROM dbo.fct_revenue_summary",
     "terms": ["Cancelled Revenue"]},
    {"name": "gross_revenue_usd", "displayName": "Gross Revenue (USD)",
     "description": "Net plus cancelled revenue.",
     "metricType": "SUM", "unitOfMeasurement": "DOLLARS", "granularity": "DAY",
     "expression": "SELECT SUM(revenue_usd + cancelled_revenue_usd) FROM dbo.fct_revenue_summary",
     "terms": ["Gross Revenue"]},
    {"name": "cancellation_rate", "displayName": "Cancellation rate",
     "description": "Cancelled revenue as a share of gross revenue.",
     "metricType": "RATIO", "unitOfMeasurement": "PERCENTAGE", "granularity": "MONTH",
     "expression": "SELECT SUM(cancelled_revenue_usd) / NULLIF(SUM(revenue_usd + cancelled_revenue_usd), 0) FROM dbo.fct_revenue_summary",
     "terms": ["Cancelled Revenue", "Gross Revenue"]},
    {"name": "units_sold", "displayName": "Units sold",
     "description": "Quantity on non-cancelled sale lines.",
     "metricType": "SUM", "unitOfMeasurement": "COUNT", "granularity": "DAY",
     "expression": "SELECT SUM(units) FROM dbo.fct_revenue_summary",
     "terms": ["Net Revenue"]},
    {"name": "average_order_value_usd", "displayName": "Average order value (USD)",
     "description": "Net revenue per non-cancelled sale line.",
     "metricType": "AVERAGE", "unitOfMeasurement": "DOLLARS", "granularity": "MONTH",
     "expression": "SELECT SUM(amount_usd) / COUNT(*) FROM dbo.fct_sales WHERE is_cancelled = 0",
     "terms": ["Net Revenue"]},
]

TABLES = {
    "dim_country": "Reference list of the three trading countries (US, GB, SG).",
    "dim_customer": "Customers as the POS system knows them, with marketing segment and loyalty tier. For cross-channel counts use dim_party.",
    "dim_date": "Calendar with Contoso's fiscal attributes (FY starts 1 April). Join on date_key = CAST(order_date AS date).",
    "dim_party": "Customers resolved across POS and web by email (in_pos / in_web). marketing_segment is NULL for web-only shoppers.",
    "dim_product": "Product hierarchy: category, department and the group data office's product segment.",
    "fct_daily_revenue": "Net revenue by trading day and country — 'how are we trading', not a management pack.",
    "fct_orders": "Order lines from the POS customer view: one row per order line with local-currency amount and its USD conversion.",
    "fct_revenue_summary": "THE MANAGEMENT REPORTING AGGREGATE: net and cancelled revenue by fiscal period, channel system, department, product segment, customer segment and country. Use this first for revenue questions.",
    "fct_sales": "Sale lines across BOTH selling systems keyed by resolved party; is_cancelled marks web cancellations.",
}

COLUMNS = {
    "fct_revenue_summary.customer_segment": "Customer Segment; 'Unsegmented' for web-only shoppers.",
    "fct_revenue_summary.country": "Customer country; 'Unknown' where the party has none.",
    "fct_revenue_summary.sale_lines": "Count of sale lines INCLUDING cancelled ones.",
    "fct_revenue_summary.units": "Units on non-cancelled lines.",
    "fct_sales.is_cancelled": "1 if the web storefront cancelled the line.",
    "fct_sales.amount_usd": "Line amount converted to USD at rate_to_usd.",
    "fct_orders.status": "settled | cancelled | pending.",
    "fct_orders.order_date": "ISO date string (varchar); cast to date before joining dim_date.",
    "fct_sales.order_date": "ISO date string (varchar); cast to date before joining dim_date.",
}

# Primary / foreign keys: the "strong deterministic signals" for SQL generation.
KEYS = {
    "dim_country": {"pk": ["country"]},
    "dim_customer": {"pk": ["customer_id"], "fk": [("country", "dim_country.country")]},
    "dim_date": {"pk": ["date_key"]},
    "dim_party": {"pk": ["party_key"], "fk": [("pos_customer_id", "dim_customer.customer_id")]},
    "dim_product": {"pk": ["product_id"]},
    "fct_orders": {"pk": ["order_id"], "fk": [("customer_id", "dim_customer.customer_id"),
                                               ("product_id", "dim_product.product_id")]},
    "fct_sales": {"pk": ["sale_id"], "fk": [("party_key", "dim_party.party_key"),
                                             ("product_id", "dim_product.product_id")]},
    "fct_revenue_summary": {"fk": [("country", "dim_country.country")]},
    "fct_daily_revenue": {"fk": [("country", "dim_country.country")]},
}
