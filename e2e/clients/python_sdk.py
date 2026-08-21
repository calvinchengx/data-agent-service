"""Connect with the reference Python MCP SDK, not with our own client.

Our agent uses a small hand-written MCP client, which proves the server answers
the calls WE make. That is not the same as being usable by any client. This
connects with `mcp`, the reference implementation, over Streamable HTTP with a
bearer token, and does what a real client does on connect: initialize, list
tools, call one.
"""
from __future__ import annotations

import asyncio
import json
import sys

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


async def main(url: str, token: str) -> int:
    headers = {"Authorization": f"Bearer {token}"}
    async with streamablehttp_client(url, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            info = await session.initialize()
            print(f"  server: {info.serverInfo.name} {info.serverInfo.version} "
                  f"(protocol {info.protocolVersion})")
            listing = await session.list_tools()
            names = sorted(t.name for t in listing.tools)
            print(f"  tools: {names}")
            result = await session.call_tool(
                "run_query",
                {"sql": "SELECT COUNT(*) AS n FROM dbo.fct_revenue_summary"})
            text = "".join(c.text for c in result.content if c.type == "text")
            payload = json.loads(text)
            print(f"  run_query: {payload['rowCount']} row, "
                  f"tables {payload['tables']}, isError={result.isError}")
            ok = (not result.isError) and payload["rowCount"] == 1 and names
            refusal = await session.call_tool("run_query", {"sql": "DROP TABLE dbo.fct_sales"})
            refused = refusal.isError and "read-only" in "".join(
                c.text for c in refusal.content if c.type == "text")
            print(f"  guard reaches this client as a tool error: {refused}")
            return 0 if (ok and refused) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1], sys.argv[2])))
