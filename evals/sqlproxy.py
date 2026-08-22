"""Execute the scorer's SQL from inside the compose network.

The eval harness runs on the HOST when the agent is the `claude` CLI, because
that is where the CLI and its credential live. The scorer, though, has to open
each source directly: it runs the reference SQL and re-runs whatever the agent
ran, and compares the two result sets.

For PostgreSQL that works from the host — a published port and an address is
all it takes. For the Fabric warehouse it does not, and not for a reason a port
mapping can fix: the engine addresses a warehouse by the WORKSPACE encoded in
its server name, which is what the compose DNS alias provides. Rewriting that
name to a container address reaches the engine and loses the routing:

    database "contoso_warehouse" not found by id; to address a warehouse or
    lakehouse by name, put the workspace in the server name

So the SQL runs where the name resolves. This is a line-oriented server —
`{"source": ..., "sql": ...}` in, `{"columns": [...], "rows": [...]}` out —
started once and kept open, because a container per query would cost more than
the queries.

Nothing here is a service code path. It exists so a harness can straddle a
network boundary that production does not have: there, the harness and the
engine are both simply on the network.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from seed import common as c


def sources() -> dict[str, dict]:
    return {s["name"]: s for s in json.loads(c.CFG.get("DAS_SOURCES", "[]"))}


def main() -> int:
    configured = sources()
    # `Any`, not `object`: these are database connections from three different
    # drivers with no shared base class, and the only thing the code needs of
    # them is that they answer .cursor().
    open_connections: dict[str, Any] = {}
    print(json.dumps({"ready": sorted(configured)}), flush=True)

    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
            name = request["source"]
            if name not in open_connections:
                open_connections[name] = c.connect_source(configured[name])
            cursor = open_connections[name].cursor()
            cursor.execute(request["sql"])
            description = cursor.description or []
            columns = [d[0] for d in description]
            rows = [[_jsonable(v) for v in row] for row in cursor.fetchall()]
            answer = {"columns": columns, "rows": rows}
        except Exception as e:  # noqa: BLE001 — the caller scores the failure
            # Reported rather than raised: a statement that will not run is a
            # result the scorer needs, not a reason to take the harness down
            # halfway through a paid run.
            answer = {"error": f"{type(e).__name__}: {e}"}
        print(json.dumps(answer, default=str), flush=True)
    return 0


def _jsonable(value):
    import datetime
    import decimal

    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, datetime.date | datetime.datetime | datetime.time):
        return value.isoformat()
    if isinstance(value, bytes | bytearray):
        return value.hex()
    return value


if __name__ == "__main__":
    raise SystemExit(main())
