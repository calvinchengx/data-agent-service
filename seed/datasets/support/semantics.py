"""Contoso Support — the semantic layer for the second source.

The definition that matters is `Resolution Time`. Wall-clock elapsed time and
resolution time differ by the period a ticket spent waiting on the customer,
and on this data that is a 44% gap — so an agent that reads the column names
and stops gets a plausible, confidently wrong answer, and a different ranking
of teams.

The descriptions carry the MAGNITUDE, not only the rule ("mean elapsed 369
minutes against mean resolution 256"). A definition with a number in it gives
a model something to check its own answer against; a definition that only
states a rule does not.
"""

SERVICE = "postgres_support"
DOMAIN = {
    "name": "contoso-support",
    "displayName": "Contoso Support",
    "domainType": "Consumer-aligned",
    "description": "Customer support operations: tickets, agents and service levels.",
}
DATA_PRODUCT = {
    "name": "contoso-support-desk",
    "displayName": "Contoso Support Desk",
    "description": "Ticket operations reporting for the support organisation.",
}

GLOSSARY = {
    "name": "Contoso Support",
    "description": "Business vocabulary for Contoso's support operations.",
}

TERMS = {
    "Resolution Time": {
        "description": (
            "Minutes a ticket took to resolve, **excluding** any period it spent waiting on "
            "the customer: `resolution_minutes = elapsed_minutes - waiting_minutes`. Elapsed "
            "time is wall-clock and is NOT the answer to 'how long did we take' — on current "
            "data mean elapsed is 369 minutes against mean resolution of 256, a 44% "
            "difference, and the two orderings of teams disagree. Always use "
            "`resolution_minutes`; use `elapsed_minutes` only when the question is explicitly "
            "about wall-clock duration."
        ),
        "synonyms": ["handling time", "time to resolve"],
        "columns": [
            "tickets.resolution_minutes",
            "tickets.elapsed_minutes",
            "tickets.waiting_minutes",
        ],
    },
    "Service Level Target": {
        "description": (
            "The minutes allowed for a priority, from the `sla` table: P1 60, P2 240, P3 1440. "
            "A ticket breaches when its Resolution Time exceeds the target for its priority — "
            "so a breach is measured against resolution, never against elapsed time, or "
            "customers who were slow to reply would count against the team."
        ),
        "synonyms": ["SLA", "target", "breach threshold"],
        "columns": ["sla.target_minutes", "sla.priority", "tickets.priority"],
    },
    "Open Ticket": {
        "description": (
            "A ticket with status 'open': it has no `resolved_at`, and its "
            "`resolution_minutes` is NULL. Averages over resolution time therefore describe "
            "resolved tickets only, and a count of tickets is not a count of resolved ones."
        ),
        "synonyms": ["unresolved"],
        "columns": ["tickets.status", "tickets.resolved_at"],
    },
    "Support Team": {
        "description": "The group an agent belongs to: Frontline, Billing or Technical.",
        "synonyms": ["team"],
        "columns": ["agents.team"],
    },
    "Channel": {
        "description": "How the ticket arrived: email, chat or phone.",
        "synonyms": [],
        "columns": ["tickets.channel"],
    },
}

METRICS = [
    {
        "name": "mean_resolution_minutes",
        "displayName": "Mean resolution time (minutes)",
        "description": "Average Resolution Time over resolved tickets — waiting time excluded.",
        "metricType": "AVERAGE",
        "unitOfMeasurement": "COUNT",
        "granularity": "DAY",
        "expression": (
            "SELECT AVG(resolution_minutes) FROM support.tickets WHERE status = 'resolved'"
        ),
        "terms": ["Resolution Time"],
    },
    {
        "name": "sla_breach_rate",
        "displayName": "SLA breach rate",
        "description": (
            "Share of resolved tickets whose Resolution Time exceeded the Service "
            "Level Target for their priority."
        ),
        "metricType": "RATIO",
        "unitOfMeasurement": "PERCENTAGE",
        "granularity": "MONTH",
        "expression": (
            "SELECT AVG(CASE WHEN t.resolution_minutes > s.target_minutes THEN 1.0 "
            "ELSE 0.0 END) FROM support.tickets t JOIN support.sla s "
            "ON s.priority = t.priority WHERE t.status = 'resolved'"
        ),
        "terms": ["Service Level Target", "Resolution Time"],
    },
    {
        "name": "open_tickets",
        "displayName": "Open tickets",
        "description": "Count of tickets not yet resolved.",
        "metricType": "COUNT",
        "unitOfMeasurement": "COUNT",
        "granularity": "DAY",
        "expression": "SELECT COUNT(*) FROM support.tickets WHERE status = 'open'",
        "terms": ["Open Ticket"],
    },
]

TABLES = {
    "customers": "Customers as the support desk knows them, with their plan and country.",
    "agents": "Support agents and the team each belongs to.",
    "sla": "Minutes allowed per priority before a ticket breaches its Service Level Target.",
    "tickets": (
        "One row per ticket. `elapsed_minutes` is wall-clock; `resolution_minutes` "
        "excludes customer-waiting time and is the figure to report."
    ),
}

# A description says what a column means; a DISPLAY NAME says what to call it.
# Both are needed and they are not the same job: all three minute columns are
# tagged with the Resolution Time glossary term, because all three take part in
# computing it, so a term cannot name any one of them. Anything generated from
# this catalog — a dashboard title, a report header — needs a name that belongs
# to the column alone.
COLUMNS = {
    "tickets.elapsed_minutes": (
        (
            "Wall-clock minutes from opened to resolved. NOT the answer to "
            "'how long did we take' — see Resolution Time."
        ),
        "Elapsed Time",
    ),
    "tickets.waiting_minutes": (
        "Minutes the ticket spent waiting on the customer.",
        "Waiting Time",
    ),
    "tickets.resolution_minutes": (
        "elapsed_minutes minus waiting_minutes. NULL while open.",
        "Resolution Time",
    ),
    "tickets.status": "resolved | open.",
    "tickets.resolved_at": "NULL while the ticket is open.",
}

KEYS = {
    "customers": {"pk": ["customer_id"]},
    "agents": {"pk": ["agent_id"]},
    "sla": {"pk": ["priority"]},
    "tickets": {
        "pk": ["ticket_id"],
        "fk": [
            ("customer_id", "customers.customer_id"),
            ("agent_id", "agents.agent_id"),
            ("priority", "sla.priority"),
        ],
    },
}
