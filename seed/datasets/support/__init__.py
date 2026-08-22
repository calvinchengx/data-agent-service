"""Contoso Support — the second use case, deliberately unlike the first.

The point of this dataset is not more data. It is to answer a question the
Contoso warehouse cannot: **does pointing the service at a different asset
actually work, or does it only look like it would?** So everything about it is
different where difference is what gets tested:

  * a different **engine** (PostgreSQL, not Fabric) and therefore a different
    SQL dialect — `LIMIT`, not `TOP`;
  * a different **schema name** (`support`, not `dbo`), so anything that had
    quietly assumed `dbo` fails;
  * a different **identity model** — see `authz_tier` in the source entry: a
    PostgreSQL with no Entra trust is queried as the service, and that is
    weaker in a way the audit records;
  * a different **business rule** whose answer changes if you ignore it, which
    is what makes the catalog worth reading rather than decorative.

That last one is the substance. Support resolution time **excludes** the period
a ticket spent waiting on the customer. An agent who subtracts nothing gets a
different, larger number and a different ranking of teams (Billing is LAST
on wall-clock and FIRST on resolution) — so a question about
resolution time is a question you cannot answer correctly from the column names
alone.
"""

from __future__ import annotations

import datetime as dt
import random
from typing import NamedTuple

ENGINE = "postgres"
SCHEMA = "support"
SOURCE_NAME = "contoso_support"

# name -> ordered (column, postgres type)
COLUMNS: dict[str, list[tuple[str, str]]] = {
    "customers": [
        ("customer_id", "varchar(12)"),
        ("name", "varchar(120)"),
        ("country", "varchar(2)"),
        ("plan", "varchar(16)"),
        ("email", "varchar(200)"),
    ],
    "agents": [
        ("agent_id", "varchar(12)"),
        ("name", "varchar(120)"),
        ("team", "varchar(32)"),
        ("email", "varchar(200)"),
    ],
    "sla": [
        ("priority", "varchar(8)"),
        ("target_minutes", "integer"),
    ],
    "tickets": [
        ("ticket_id", "varchar(14)"),
        ("customer_id", "varchar(12)"),
        ("agent_id", "varchar(12)"),
        ("priority", "varchar(8)"),
        ("channel", "varchar(12)"),
        ("status", "varchar(12)"),
        ("opened_at", "timestamp"),
        ("resolved_at", "timestamp"),
        ("waiting_minutes", "integer"),
        ("elapsed_minutes", "integer"),
        ("resolution_minutes", "integer"),
    ],
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


class TeamProfile(NamedTuple):
    """How a team's work behaves.

    A named tuple rather than a dict because the three fields have three
    different types — a scale, a probability and a range — and a dict of mixed
    values makes every use of them unprovable.
    """

    elapsed: float  # scales wall-clock duration
    waits: float  # chance a ticket ever waits on the customer
    share: tuple[float, float]  # fraction of elapsed time it waits for


# Billing is slow on the clock and fast on the work: its tickets sit waiting on
# the customer far more (invoice number, PO, authorised signatory), so it looks
# worst on elapsed time and best on resolution time. That reversal is what makes
# the catalog's definition observable rather than merely arithmetic.
TEAM_DELAY = {
    "Frontline": TeamProfile(elapsed=1.00, waits=0.20, share=(0.05, 0.20)),
    "Technical": TeamProfile(elapsed=1.25, waits=0.45, share=(0.15, 0.40)),
    "Billing": TeamProfile(elapsed=1.90, waits=0.85, share=(0.55, 0.80)),
}

PRIORITIES = [("P1", 60), ("P2", 240), ("P3", 1440)]
TEAMS = ["Frontline", "Billing", "Technical"]
CHANNELS = ["email", "chat", "phone"]
PLANS = ["free", "standard", "enterprise"]
COUNTRIES = ["US", "GB", "SG"]

START = dt.datetime(2025, 7, 1)
END = dt.datetime(2026, 6, 30)


def ddl(table: str) -> str:
    cols = ",\n  ".join(f"{name} {type_}" for name, type_ in COLUMNS[table])
    return f"CREATE TABLE {SCHEMA}.{table} (\n  {cols}\n)"


def generate(n_customers=120, n_agents=18, n_tickets=3000, seed=20260822) -> dict[str, list[tuple]]:
    rnd = random.Random(seed)
    out: dict[str, list[tuple]] = {}

    out["sla"] = [(p, minutes) for p, minutes in PRIORITIES]

    customers = [
        (
            f"C{i + 1:05d}",
            f"Support customer {i + 1}",
            rnd.choice(COUNTRIES),
            rnd.choices(PLANS, [4, 5, 2])[0],
            f"support{i + 1}@example.com",
        )
        for i in range(n_customers)
    ]
    out["customers"] = customers

    agents = [
        (f"A{i + 1:04d}", f"Agent {i + 1}", TEAMS[i % len(TEAMS)], f"agent{i + 1}@contoso.example")
        for i in range(n_agents)
    ]
    out["agents"] = agents

    tickets = []
    span = int((END - START).total_seconds())
    for i in range(n_tickets):
        customer = rnd.choice(customers)
        agent = rnd.choice(agents)
        priority, _target = rnd.choices(PRIORITIES, [1, 3, 6])[0]
        opened = START + dt.timedelta(seconds=rnd.randrange(span))
        # Elapsed is wall-clock; waiting is the part the customer owed us. The
        # difference between the two is the whole point of this dataset — and
        # it is not spread evenly across the teams, which is what makes the
        # difference OBSERVABLE rather than merely arithmetic.
        #
        # Billing tickets sit waiting on the customer far more than any other
        # kind: they need an invoice number, a PO, an authorised signatory. So
        # Billing looks like the SLOWEST team on wall-clock and is in fact the
        # FASTEST once the customer's own delay is excluded. An agent that
        # reads `elapsed_minutes` therefore gets a different winner, not just a
        # bigger number — and a wrong ranking is far harder to wave away than a
        # wrong magnitude.
        profile = TEAM_DELAY[agent[2]]
        elapsed = max(5, int(rnd.lognormvariate(5.2, 0.9) * profile.elapsed))
        waiting = (
            0
            if rnd.random() > profile.waits
            else min(elapsed - 1, int(elapsed * rnd.uniform(*profile.share)))
        )
        status = rnd.choices(["resolved", "resolved", "resolved", "open"], [6, 3, 3, 2])[0]
        resolved = opened + dt.timedelta(minutes=elapsed) if status == "resolved" else None
        tickets.append(
            (
                f"T{i + 1:07d}",
                customer[0],
                agent[0],
                priority,
                rnd.choice(CHANNELS),
                status,
                opened,
                resolved,
                waiting,
                elapsed if resolved else None,
                (elapsed - waiting) if resolved else None,
            )
        )
    out["tickets"] = tickets
    return out


if __name__ == "__main__":
    data = generate()
    for table, rows in data.items():
        print(f"{table:12s} {len(rows):6d}  {rows[0]}")
