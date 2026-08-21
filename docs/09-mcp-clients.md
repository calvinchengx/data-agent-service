# MCP clients

The surface is plain MCP: Streamable HTTP, JSON-RPC 2.0, tools described by
JSON Schema, no vendor extensions. Anything that speaks MCP can use it.

```sh
make test ARGS="--only phase10"          # 24 client-compatibility checks
make client-config                        # paste-ready configuration per client
make client-config ARGS="--auth token"    # embed a bearer instead of OAuth
```

## What is verified, and how

| Evidence | What it rules out |
|---|---|
| **Protocol suite** (hand-built JSON-RPC) | version negotiation that echoes whatever it is asked; wrong error codes; a `null` body on a notification; a schema a client cannot generate a form from |
| **The official `mcp` SDK** drives a full session | that the server only works with a client written to match it. The SDK has no knowledge of this service and no reason to be accommodating |
| **Discovery**, followed from the challenge itself | a client that cannot find out how to authenticate. The check reads `WWW-Authenticate` and fetches the URL it names, from where a client stands — testing the document on the service behind the gateway proves nothing about the URL a client is actually given |
| **Generated configuration** | a README that names a URL which stopped being true |

Both MCP endpoints are covered: `/warehouse/mcp` (our executor, proxied) and
`/om/mcp` (OpenMetadata's own server, proxied with a read-only bot).

## Two ways a client authenticates

**OAuth 2.0.** The client gets an unauthenticated 401 carrying
`WWW-Authenticate: Bearer resource_metadata=…`, reads our protected-resource
document, follows it to the tenant, and signs the user in with authorization
code + PKCE. Every hop is standard.

The metadata URL follows RFC 9728 §3.1 — the well-known segment goes between
the host and the resource's path
(`https://host/.well-known/oauth-protected-resource/warehouse/mcp`), not after
the path. That is easy to get backwards, and the mistake is invisible until a
real client follows the challenge into a 404, so the suite follows it too.

**A bearer header.** For headless or unattended clients, or any client whose
OAuth support does not fit. `make client-config ARGS="--auth token"` emits it.

### The one gap worth knowing: no dynamic client registration

Microsoft Entra implements no RFC 7591 registration endpoint, so a client
**cannot invent its own identity** against this resource — it must use a client
id registered in the tenant. This is a property of Entra, not of this service,
and it is the single thing most likely to surprise someone connecting a client
that expects to self-register.

Two consequences, both handled rather than hidden:

* our protected-resource document says `client_registration_required: false`
  and offers a `client_id_hint`, so a client can be pointed at the right
  identity instead of failing at a registration endpoint that does not exist;
* the resource server publishes **no copy of the authorization server's
  metadata**. A client reads that document from the authorization server it was
  pointed at; a copy here would be a third place for the same facts — endpoints,
  grant types, whether registration exists — to disagree.

A client that requires self-registration needs a bearer header instead.

## Client matrix

| Client | Transport | Auth | Status |
|---|---|---|---|
| Official `mcp` SDK (Python) | Streamable HTTP | header / OAuth | **witnessed** — full session, tool call, refusal, in `make test` |
| Claude Code | Streamable HTTP | OAuth or `--header` | config generated; OAuth needs the pre-registered client id |
| Claude Desktop, Cursor, VS Code | Streamable HTTP | OAuth or headers | config generated |
| Any MCP SDK (TypeScript, others) | Streamable HTTP | header / OAuth | same protocol as the witnessed SDK |
| Hosted connectors (e.g. ChatGPT) | Streamable HTTP | OAuth | **production only** — requires a publicly reachable HTTPS endpoint, which a local stack does not have |

The last row is a property of the deployment, not the protocol. It is listed as
a production check in `docs/10-production.md` rather than quietly passed here:
a suite that claims to have verified something it cannot reach is worse than
one that says it did not.

## Reaching the stack from a client on your machine

The generated configuration uses whatever the service is configured with, which
inside the compose network is `https://apim-emulator:8445`. A client running on
your machine needs the published address, and a hosted client needs a public
one:

```sh
DAS_PUBLIC_BASE_URL=https://localhost:8446 make client-config
```

The gateway serves a self-signed certificate locally, so a client that pins
certificates will refuse it — that is the certificate's fault and it goes away
in production, where `ENV=prod` points at real hosts.
