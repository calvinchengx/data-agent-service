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


def resolve_in_network(hostname: str) -> str:
    """The address a compose service answers on, resolved the way the network
    resolves it.

    Not by mapping service names to containers: a source can be reached
    through a DNS ALIAS (the Fabric warehouse is
    `contoso-analytics.datawarehouse.fabric.microsoft.com`, which is the
    address Fabric advertises and which compose aliases onto the emulator).
    Only the network knows what that resolves to, so it is asked.
    """
    out = subprocess.run(
        [
            "docker",
            "compose",
            "--profile",
            "tools",
            "run",
            "--rm",
            "-T",
            "tools",
            "python",
            "-c",
            f"import socket; print(socket.gethostbyname({hostname!r}))",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    for line in reversed(out.stdout.splitlines()):
        if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", line.strip()):
            return line.strip()
    return ""


def main() -> int:
    sources = json.loads(c.CFG.get("DAS_SOURCES", "[]"))
    resolved: dict[str, str] = {}

    def address_of(host: str) -> str:
        if host not in resolved:
            resolved[host] = resolve_in_network(host)
        return resolved[host]

    for source in sources:
        dsn = source.get("dsn", "")
        match = re.search(r"@([^:/]+)", dsn) if dsn else None
        if match and (ip := address_of(match.group(1))):
            source["dsn"] = dsn.replace(f"@{match.group(1)}", f"@{ip}")
        # A TDS source names `host:port`; only the host is rewritten, because
        # the port is the engine's, not the network's.
        if server := source.get("tds_server", ""):
            host, _, port = server.partition(":")
            if ip := address_of(host):
                source["tds_server"] = f"{ip}:{port}" if port else ip

    print(json.dumps(sources))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
