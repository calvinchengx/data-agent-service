#!/usr/bin/env python3
"""Rewrite DAS_SOURCES so a HOST process can reach each engine.

The eval scorer opens every source directly, to run the reference SQL and
compare result sets. Inside the compose network those addresses are service
names; from the host they are not resolvable, and `localhost` is worse than
useless when a host-local server already holds the port — a loopback bind wins
over docker's wildcard publish, so the scorer connects to the wrong database
and reports a missing role rather than a wrong address.

Container addresses sidestep both problems. This is a development convenience
for running the harness outside the network; nothing in the service reads it.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys

sys.path.insert(0, ".")
from seed import common as c


def container_ip(service: str) -> str:
    cid = (
        subprocess.run(
            ["docker", "compose", "ps", "-q", service],
            capture_output=True,
            text=True,
            check=False,
        )
        .stdout.strip()
        .splitlines()
    )
    if not cid:
        return ""
    return subprocess.run(
        [
            "docker",
            "inspect",
            "-f",
            "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
            cid[0],
        ],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()


def main() -> int:
    sources = json.loads(c.CFG.get("DAS_SOURCES", "[]"))
    for source in sources:
        dsn = source.get("dsn", "")
        if dsn:
            host = re.search(r"@([^:/]+)", dsn)
            if host and (ip := container_ip(host.group(1))):
                source["dsn"] = dsn.replace(f"@{host.group(1)}", f"@{ip}")
    print(json.dumps(sources))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
