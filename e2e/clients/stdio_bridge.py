"""A stdio front door for the gateway's MCP endpoints.

    docker compose run --rm -T tools python -m e2e.clients.stdio_bridge --server warehouse

Some MCP clients speak only stdio: they launch a command and exchange
newline-delimited JSON-RPC over its standard input and output. This forwards
that conversation to the Streamable HTTP endpoint the gateway publishes.

It is a TRANSPARENT proxy, not a re-implementation. Whatever the client sends
is what the server receives, and whatever the server answers is what the
client reads — so tool schemas, protocol version negotiation, error codes and
refusals all pass through exactly as the server meant them. A bridge that
rebuilt those would be a second implementation of the surface, and the first
thing it would do is disagree with the real one.

It exists because of where the client runs, not because of what it is. Inside
the compose network the bridge resolves the gateway's hostname, trusts the
development certificate, and signs in as a persona — three things a desktop
application on the host cannot do without an entry in /etc/hosts, a
certificate in the system keychain, and matching published ports. In
production none of that applies: the endpoint is a public HTTPS URL with a
real certificate and a client connects to it directly.

The bridge grants nothing. It signs in as one persona, the engine applies THAT
user's permissions, and every refusal still comes from the executor.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import ssl
import sys
import urllib.error
import urllib.request

from agent import identity
from seed import common as c

ENDPOINTS = {
    "warehouse": ("DAS_WAREHOUSE_MCP_PATH", "/warehouse/mcp"),
    "catalog": ("DAS_OM_MCP_PATH", "/om/mcp"),
}


def log(message: str) -> None:
    """Diagnostics go to stderr: stdout is the protocol channel, and a stray
    line there is a parse error at the other end."""
    print(f"bridge: {message}", file=sys.stderr, flush=True)


# Field names that carry MEANING rather than structure. Stripping exactly
# these is what separates "the catalog is absent" from "the catalog holds no
# semantics" — see docs/07-evaluation.md on the schema-only arm.
SEMANTIC_FIELDS = frozenset(
    {
        "description",
        "displayName",
        "tags",
        "glossary",
        "glossaryTerms",
        "synonyms",
        "expression",
        "unitOfMeasurement",
        "relatedTerms",
        "owners",
    }
)


def strip_semantics(value):
    """Remove meaning, keep structure.

    An ablation that removes the catalog SERVER removes knowledge and tools at
    once, so its delta cannot say which one mattered. This leaves the tools
    exactly where they were — same names, same schemas, same call sequence —
    and empties the fields that carry business meaning. What remains is what a
    catalog looks like when nobody has described anything in it, which is also
    the state most real catalogs are in before someone does the work.
    """
    if isinstance(value, dict):
        return {
            k: ("" if k in SEMANTIC_FIELDS else strip_semantics(v))
            for k, v in value.items()
            if not (k in SEMANTIC_FIELDS and isinstance(v, list))
        }
    if isinstance(value, list):
        return [strip_semantics(v) for v in value]
    return value


class Upstream:
    def __init__(self, name: str, user: str, *, strip: bool = False):
        self.strip = strip
        variable, default = ENDPOINTS[name]
        base = os.environ["DAS_APIM_BASE"].rstrip("/")
        self.url = base + os.environ.get(variable, default)
        self.headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": "Bearer " + identity.token_for(user),
        }
        if name == "catalog":
            # Resolved, not read: the setting holds a `keyvault:` reference,
            # and sending the reference string as the key is a 401 that says
            # SubscriptionKeyInvalid — which reads as a wrong key rather than
            # an unresolved one.
            key = c.setting("DAS_OM_SUBSCRIPTION_KEY")
            if key:
                self.headers["Ocp-Apim-Subscription-Key"] = key
        self.ssl = ssl.create_default_context()
        if os.environ.get("DAS_ENTRA_TLS_INSECURE", "false").lower() in ("1", "true", "yes"):
            self.ssl.check_hostname = False
            self.ssl.verify_mode = ssl.CERT_NONE
        self.session: str | None = None

    def send(self, message: dict) -> dict | None:
        """Forward one message. Returns the server's answer, or None for a
        notification, which by definition is not answered."""
        headers = dict(self.headers)
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        request = urllib.request.Request(
            self.url, data=json.dumps(message).encode(), headers=headers, method="POST"
        )
        with urllib.request.urlopen(request, context=self.ssl, timeout=120) as response:
            session = response.headers.get("Mcp-Session-Id")
            if session:
                self.session = session
            raw = response.read().decode().strip()
        if not raw:
            return None
        if raw.startswith(("{", "[")):
            return self._maybe_strip(json.loads(raw))
        # Streamable HTTP may answer with SSE frames; take the first data line.
        for line in raw.splitlines():
            if line.startswith("data:"):
                chunk = line[5:].strip()
                if chunk:
                    return self._maybe_strip(json.loads(chunk))
        return None

    def _maybe_strip(self, payload):
        """Redact on the way OUT, so the client sees a catalog with no meaning.

        Tool RESULTS carry the semantics; tool definitions carry the surface.
        Both pass through here, and stripping descriptions from the tool list
        too would change what the model knows the tools are FOR — which is
        method, not meaning, and would confound the very thing being isolated.
        """
        if not self.strip or not isinstance(payload, dict):
            return payload
        result = payload.get("result")
        if isinstance(result, dict) and "content" in result:
            for block in result.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "text":
                    with contextlib.suppress(json.JSONDecodeError, TypeError):
                        block["text"] = json.dumps(strip_semantics(json.loads(block["text"])))
        return payload


def pump(upstream: Upstream) -> None:
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            log("ignored a line that was not JSON")
            continue
        try:
            answer = upstream.send(message)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            # Report the failure in the client's own protocol rather than
            # dying: a dead bridge looks like a server with no tools.
            if message.get("id") is None:
                continue
            answer = {
                "jsonrpc": "2.0",
                "id": message["id"],
                "error": {"code": -32001, "message": f"the gateway could not be reached: {e}"},
            }
        if answer is None:
            continue
        sys.stdout.write(json.dumps(answer) + "\n")
        sys.stdout.flush()


def main() -> int:
    ap = argparse.ArgumentParser(description="Bridge a stdio MCP client to the gateway.")
    ap.add_argument("--server", choices=sorted(ENDPOINTS), default="warehouse")
    ap.add_argument(
        "--strip-semantics",
        action="store_true",
        help="serve the catalog with its meaning removed: same tools, empty descriptions",
    )
    ap.add_argument(
        "--user",
        default=os.environ.get("DAS_BRIDGE_USER", "carol@entraemulator.dev"),
        help="which persona to sign in as; the engine applies THAT user's permissions",
    )
    a = ap.parse_args()
    upstream = Upstream(a.server, a.user, strip=a.strip_semantics)
    log(
        f"{a.server} as {a.user} -> {upstream.url}"
        + (" [semantics stripped]" if a.strip_semantics else "")
    )
    pump(upstream)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
