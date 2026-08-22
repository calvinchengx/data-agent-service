"""`make client-config` — ready-to-paste configuration for MCP clients.

    python -m e2e.clients.configs                  # every client
    python -m e2e.clients.configs --client cursor
    python -m e2e.clients.configs --auth token     # embed a bearer instead of OAuth

Generated from the running configuration rather than written down, because a
README that names a URL is wrong the first time somebody changes a port.

Two ways in, and which one a client can use is the whole story of
docs/09-mcp-clients.md:

  * **oauth** — the client discovers the authorization server from our
    protected-resource metadata and signs the user in. It needs a client id,
    because Entra implements no dynamic client registration.
  * **token** — a bearer the client sends as a header. Every client supports
    this, and it is how a headless or unattended one connects.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys

from seed import common as c

GW = c.CFG["DAS_APIM_BASE"].rstrip("/")
PUBLIC = c.CFG.get("DAS_PUBLIC_BASE_URL", GW).rstrip("/")
WAREHOUSE = PUBLIC + c.CFG.get("DAS_WAREHOUSE_MCP_PATH", "/warehouse/mcp")
CATALOG = PUBLIC + c.CFG.get("DAS_OM_MCP_PATH", "/om/mcp")
# Resolved, not read: the setting holds a `keyvault:` reference, and a
# generated client config has to carry the real key -- a person pastes it into
# Claude Desktop or Cursor, which have no identity to resolve one with. This is
# the boundary where a literal is unavoidable, and it is explicit.
OM_KEY = c.setting("DAS_OM_SUBSCRIPTION_KEY")
CLIENT_ID = c.CFG.get("DAS_AGENT_CLIENT_ID", "")
AUDIENCE = c.CFG["DAS_AGENT_AUDIENCE"]
SCOPE = f"{AUDIENCE}/{c.CFG.get('DAS_REQUIRED_SCOPE', 'access_as_user')}"


def headers(auth: str, token: str, catalog: bool) -> dict:
    out = {}
    if auth == "token":
        out["Authorization"] = f"Bearer {token or '<token>'}"
    if catalog and OM_KEY:
        out["Ocp-Apim-Subscription-Key"] = OM_KEY
    return out


def servers(auth: str, token: str) -> dict:
    return {
        "warehouse": {
            "type": "http",
            "url": WAREHOUSE,
            **({"headers": headers(auth, token, False)} if headers(auth, token, False) else {}),
        },
        "catalog": {
            "type": "http",
            "url": CATALOG,
            **({"headers": headers(auth, token, True)} if headers(auth, token, True) else {}),
        },
    }


def claude_code(auth: str, token: str) -> str:
    lines = []
    for name, spec in servers(auth, token).items():
        cmd = f"claude mcp add --transport http {name} {spec['url']}"
        for key, value in (spec.get("headers") or {}).items():
            cmd += f' \\\n    --header "{key}: {value}"'
        lines.append(cmd)
    return "\n\n".join(lines)


def json_block(auth: str, token: str, key: str = "mcpServers") -> str:
    return json.dumps({key: servers(auth, token)}, indent=2)


# Three personas rather than one, because the interesting behaviour is the
# DIFFERENCE between them, and a single connection can only ever show one side
# of it. Named by the person so a conversation can say which to use.
PERSONAS = (
    (
        "alice",
        "alice@entraemulator.dev",
        "Data.Analyst — personal-data columns withheld by the rules",
    ),
    ("carol", "carol@entraemulator.dev", "Data.Finance — reads the columns the analyst cannot"),
    ("bob", "bob@entraemulator.dev", "no role on the source — refused by the ENGINE, not by us"),
)


def desktop_bridge(_auth: str, _token: str) -> str:
    """Claude Desktop, via the stdio bridge.

    Desktop talks to a REMOTE endpoint over TLS with a certificate its host
    trusts, at a hostname its host resolves. The local stack offers neither: a
    development certificate, a compose-internal hostname, and a published port
    that differs from the one the gateway advertises. Rather than ask a person
    to edit /etc/hosts and install a certificate authority to try a demo, the
    bridge runs INSIDE the network where all three already hold and speaks
    stdio, which Desktop launches directly.

    Against a real deployment this entry is not needed: the endpoint is a
    public HTTPS URL and Desktop connects to it as a remote MCP server.
    """
    # The path must be the HOST's, because Desktop runs on the host and this
    # renders inside the tools container, where the repo is /app. The Makefile
    # passes the real one; without it the output would be confidently wrong.
    root = os.environ.get("DAS_HOST_REPO") or str(pathlib.Path(__file__).resolve().parents[2])

    def entry(server: str, upn: str = "") -> dict:
        args = [
            "compose",
            "--project-directory",
            root,
            "--env-file",
            str(pathlib.Path(root) / ".env"),
            "--profile",
            "tools",
            "run",
            "--rm",
            "-T",
        ]
        if upn:
            args += ["-e", f"DAS_BRIDGE_USER={upn}"]
        args += ["tools", "python", "-m", "e2e.clients.stdio_bridge", "--server", server]
        return {"command": "docker", "args": args}

    servers = {f"warehouse-{name}": entry("warehouse", upn) for name, upn, _ in PERSONAS}
    # One catalog connection: the business context is the same whoever is
    # asking. Only the DATA is per-person, which is the point.
    servers["catalog"] = entry("catalog")
    return json.dumps({"mcpServers": servers}, indent=2)


CLIENTS = {
    "claude-code": ("Claude Code (CLI)", "shell", claude_code),
    "claude-desktop": (
        "Claude Desktop — claude_desktop_config.json (stdio bridge; see the note)",
        "json",
        desktop_bridge,
    ),
    "claude-desktop-remote": (
        "Claude Desktop — direct, for a deployment with a trusted certificate",
        "json",
        json_block,
    ),
    "cursor": ("Cursor — .cursor/mcp.json", "json", json_block),
    "vscode": ("VS Code — .vscode/mcp.json", "json", lambda a, t: json_block(a, t, "servers")),
    "sdk": (
        "Any client built on an MCP SDK",
        "python",
        lambda a, t: (
            f'''\
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
import httpx2

http = httpx2.AsyncClient(headers={{"Authorization": "Bearer <token>"}})
async with http, streamable_http_client("{WAREHOUSE}", http_client=http) as streams:
    async with ClientSession(streams[0], streams[1]) as session:
        await session.initialize()
        tools = await session.list_tools()'''
        ),
    ),
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--client", choices=sorted(CLIENTS), action="append")
    ap.add_argument("--auth", choices=("oauth", "token"), default="oauth")
    ap.add_argument("--user", default=c.CFG.get("DAS_USER", "carol@entraemulator.dev"))
    ap.add_argument("--env", default="local")
    a = ap.parse_args()

    token = ""
    if a.auth == "token":
        # A real token, obtained however this environment permits — the same
        # helper the witnesses use, so generating a configuration against a
        # tenant works where the password grant would be refused.
        from agent import identity

        try:
            token = identity.token_for(a.user)
        except Exception as e:  # noqa: BLE001 — printing a config is still useful
            print(f"could not mint a token for {a.user}: {e}", file=sys.stderr)

    if a.auth == "oauth":
        print(f"""Sign-in details a client needs (Entra has no dynamic client
registration, so the client id is configured rather than requested):

  authority     {c.AUTHORITY}
  client id     {CLIENT_ID}
  scope         {SCOPE}
  discovery     {PUBLIC}/warehouse/.well-known/oauth-protected-resource
""")

    for name in a.client or sorted(CLIENTS):
        title, kind, render = CLIENTS[name]
        print(f"\n# {title}\n")
        print(f"```{kind}\n{render(a.auth, token)}\n```")
    return 0


if __name__ == "__main__":
    sys.exit(main())
