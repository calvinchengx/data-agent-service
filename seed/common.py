"""Shared helpers for the seed steps.

Discipline rule 2: everything here is a STANDARD protocol a real tenant serves —
OAuth2 client_credentials, Microsoft Graph, the Fabric REST API (with LROs), TDS
with an Entra access token. Nothing calls an emulator's own control surface.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import ssl
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent


# ----------------------------------------------------------------- config --
def load_env(env: str = "local") -> dict[str, str]:
    """`.env` (local) or `.env.prod`, KEY=VALUE lines; process env wins."""
    f = ROOT / (".env.prod" if env == "prod" else ".env")
    if not f.exists() and env == "local":
        f = ROOT / ".env.example"
    cfg: dict[str, str] = {}
    for raw in f.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        cfg[k.strip()] = v.strip()
    cfg.update({k: v for k, v in os.environ.items() if k.startswith("DAS_") or k in cfg})
    return cfg


CFG = load_env(os.environ.get("DAS_ENV", "local"))
TENANT = CFG["DAS_TENANT_ID"]
ISSUER = CFG["DAS_ENTRA_ISSUER"].rstrip("/")
AUTHORITY = ISSUER[: -len("/v2.0")] if ISSUER.endswith("/v2.0") else ISSUER
LOGIN_ORIGIN = AUTHORITY.rsplit("/", 1)[0]
FABRIC = CFG["DAS_FABRIC_API"].rstrip("/")
OM = CFG["DAS_OM_URL"].rstrip("/")
INSECURE = CFG.get("DAS_ENTRA_TLS_INSECURE", "false").lower() == "true"
STATE = ROOT / "seed" / "state.json"

# Seed-time credential: a confidential client (client_credentials). Locally the
# emulator's seeded daemon; in prod an app registration with the needed roles.
CLIENT_ID = CFG.get("DAS_SEED_CLIENT_ID") or CFG["DAS_QUERY_SVC_CLIENT_ID"]
CLIENT_SECRET = CFG.get("DAS_SEED_CLIENT_SECRET", "daemon-app-secret")
# The platform injects this wherever a managed identity exists — the same
# variable the executor reads, so the harness and the service agree.
IDENTITY_ENDPOINT = os.environ.get("IDENTITY_ENDPOINT", "")
# GitHub Actions injects these into a job that requests `id-token: write`. The
# names are GitHub's, not ours, which is why they carry no DAS_ prefix.
OIDC_REQUEST_URL = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_URL", "")
OIDC_REQUEST_TOKEN = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "")
# What Entra calls the audience of a federated client assertion. Fixed by the
# protocol, not a setting.
EXCHANGE_AUDIENCE = "api://AzureADTokenExchange"

FABRIC_AUD = CFG.get("DAS_FABRIC_AUDIENCE", "https://api.fabric.microsoft.com")
SQL_AUD = CFG.get("DAS_SQL_AUDIENCE", "https://database.windows.net")
GRAPH_AUD = "https://graph.microsoft.com"

_SSL = ssl.create_default_context()
if INSECURE:
    _SSL.check_hostname = False
    _SSL.verify_mode = ssl.CERT_NONE


def log(msg: str) -> None:
    print(f"==> {msg}", flush=True)


# ------------------------------------------------------------------- http --
class HttpError(Exception):
    def __init__(self, status: int, body: str, url: str):
        super().__init__(f"{status} {url}\n{body[:800]}")
        self.status, self.body, self.url = status, body, url


def http(method: str, url: str, *, headers=None, json_body=None, form=None, raw=None):
    """Returns (status, headers, body_text)."""
    data = None
    h = dict(headers or {})
    if json_body is not None:
        data = json.dumps(json_body).encode()
        h.setdefault("Content-Type", "application/json")
    elif form is not None:
        data = urllib.parse.urlencode(form).encode()
        h.setdefault("Content-Type", "application/x-www-form-urlencoded")
    elif raw is not None:
        data = raw
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, context=_SSL, timeout=60) as r:
            return r.status, dict(r.headers), r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode()


def must(method, url, ok=(200, 201, 202, 204), **kw):
    st, hd, body = http(method, url, **kw)
    if st not in ok:
        raise HttpError(st, body, url)
    return st, hd, (json.loads(body) if body.strip().startswith(("{", "[")) else body)


# ----------------------------------------------------------------- tokens --
_TOK: dict[str, tuple[float, str]] = {}


def token(audience: str) -> str:
    """OAuth2 client_credentials for `<audience>/.default`, cached until near expiry.

    A supplied token wins, keyed by audience. That is what lets a harness run
    OUTSIDE the compose network: the tenant answers on a hostname only the
    network resolves, so a process on the host cannot sign in at all, even
    though it can reach the engine itself on a published port. Minting inside
    and handing the token over is the same arrangement the persona tokens
    already use, and it changes nothing for a caller that can sign in.
    """
    supplied = os.environ.get("DAS_ACCESS_TOKEN_" + re.sub(r"[^A-Z0-9]+", "_", audience.upper()))
    if supplied:
        return supplied
    exp, t = _TOK.get(audience, (0.0, ""))
    if exp - 60 > time.time():
        return t

    # A managed identity before a secret, wherever one is offered. The
    # executor has always taken this path; the harness asking for a client
    # secret instead is the reason `make eval ENV=prod` could not have worked:
    # `.env.prod.example` leaves DAS_SEED_CLIENT_SECRET deliberately empty,
    # because the alternative is a standing credential in a settings file, and
    # client_credentials with an empty secret is a 401 that says nothing useful.
    if IDENTITY_ENDPOINT:
        st, _, body = http(
            "GET",
            f"{IDENTITY_ENDPOINT}?resource={urllib.parse.quote(audience)}&api-version=2019-08-01",
            headers={"X-IDENTITY-HEADER": os.environ.get("IDENTITY_HEADER", "")},
        )
        if st == 200:
            payload = json.loads(body)
            _TOK[audience] = (time.time() + 3300, payload["access_token"])
            return payload["access_token"]

    # A federated credential before a stored one. On GitHub Actions the runner
    # is handed a short-lived OIDC token proving which repository and ref is
    # running; Entra trusts that issuer and subject, and exchanges it for an
    # app token. No secret exists at any point -- nothing to store, nothing to
    # rotate, nothing to leak. It is the same mechanism the executor's
    # on-behalf-of path uses, pointed at a different issuer.
    if OIDC_REQUEST_URL and OIDC_REQUEST_TOKEN:
        st, _, body = http(
            "GET",
            f"{OIDC_REQUEST_URL}&audience={urllib.parse.quote(EXCHANGE_AUDIENCE)}",
            headers={"Authorization": f"Bearer {OIDC_REQUEST_TOKEN}"},
        )
        if st == 200:
            assertion = json.loads(body).get("value", "")
            st, _, body = http(
                "POST",
                f"{AUTHORITY}/oauth2/v2.0/token",
                form={
                    "grant_type": "client_credentials",
                    "client_id": CLIENT_ID,
                    "client_assertion_type": (
                        "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
                    ),
                    "client_assertion": assertion,
                    "scope": f"{audience}/.default",
                },
            )
            if st == 200:
                payload = json.loads(body)
                _TOK[audience] = (
                    time.time() + int(payload.get("expires_in", 3600)) - 60,
                    payload["access_token"],
                )
                return payload["access_token"]
            log(f"federated exchange for {audience} failed ({st}); falling through")

    if not CLIENT_SECRET:
        raise SystemExit(
            f"no way to obtain a token for {audience}.\n"
            "  - in Azure: run where a managed identity is available "
            "(IDENTITY_ENDPOINT), which needs no secret at all;\n"
            "  - on a laptop: supply one with "
            f"DAS_ACCESS_TOKEN_{re.sub(r'[^A-Z0-9]+', '_', audience.upper())}, "
            "minted however your tenant allows;\n"
            "  - in CI: a federated credential, which needs no secret either "
            "(ACTIONS_ID_TOKEN_REQUEST_URL, see docs/10-production.md);\n"
            "  - locally: DAS_SEED_CLIENT_SECRET is set by the emulator seed.\n"
            "A service principal with a checked-in secret is deliberately NOT "
            "the answer -- see docs/10-production.md."
        )

    st, _, body = http(
        "POST",
        f"{AUTHORITY}/oauth2/v2.0/token",
        form={
            "grant_type": "client_credentials",
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "scope": f"{audience}/.default",
        },
    )
    if st != 200:
        raise SystemExit(f"token for {audience}: {st} {body}")
    r = json.loads(body)
    _TOK[audience] = (time.time() + int(r.get("expires_in", 3600)), r["access_token"])
    return r["access_token"]


def bearer(audience: str) -> dict[str, str]:
    return {"Authorization": "Bearer " + token(audience)}


# ------------------------------------------------------------------ graph --
def graph_ensure_resource_app(identifier_uri: str, display_name: str) -> str:
    """Make `identifier_uri` an issuable audience via Microsoft Graph.

    In a real tenant first-party resources (database.windows.net, vault.azure.net)
    already exist, so the lookup finds them and nothing is created. A development
    tenant may lack one; the Graph call that creates it is the same call an admin
    would make for any API. Postcondition-driven: skip if present, create if not.
    """
    q = urllib.parse.quote(f"identifierUris/any(u:u eq '{identifier_uri}')")
    st, _, body = http(
        "GET",
        f"{LOGIN_ORIGIN}/graph/v1.0/applications?$filter={q}&$select=appId",
        headers=bearer(GRAPH_AUD),
    )
    if st == 200:
        vals = json.loads(body).get("value", [])
        if vals:
            return vals[0]["appId"]
    # A Graph that does not support the lambda filter (400) or found nothing: scan.
    st, _, body = must(
        "GET",
        f"{LOGIN_ORIGIN}/graph/v1.0/applications?$select=appId,identifierUris",
        headers=bearer(GRAPH_AUD),
    )
    for a in body.get("value", []):
        if identifier_uri in (a.get("identifierUris") or []):
            return a["appId"]
    st, _, body = must(
        "POST",
        f"{LOGIN_ORIGIN}/graph/v1.0/applications",
        headers=bearer(GRAPH_AUD),
        json_body={
            "displayName": display_name,
            "identifierUris": [identifier_uri],
            "signInAudience": "AzureADMyOrg",
        },
    )
    log(f"registered resource app {identifier_uri} ({body['appId']})")
    return body["appId"]


# ----------------------------------------------------------------- fabric --
def fabric_get(path: str):
    _, _, body = must("GET", f"{FABRIC}{path}", headers=bearer(FABRIC_AUD))
    return body


def fabric_post_wait(path: str, body: dict, what: str = ""):
    """POST; if 202, follow the documented LRO (operation-id or Location, Retry-After)."""
    what = what or path
    st, hd, resp = must("POST", f"{FABRIC}{path}", headers=bearer(FABRIC_AUD), json_body=body)
    if st != 202:
        return resp
    hd = {k.lower(): v for k, v in hd.items()}
    op = hd.get("x-ms-operation-id") or hd["location"].rstrip("/").rsplit("/", 1)[-1]
    for _ in range(150):
        st, h2, got = must("GET", f"{FABRIC}/v1/operations/{op}", headers=bearer(FABRIC_AUD))
        status = (got or {}).get("status")
        if status in ("Succeeded", "Failed"):
            break
        ra = {k.lower(): v for k, v in h2.items()}.get("retry-after", "2")
        time.sleep(min(float(ra), 20))
    else:
        raise SystemExit(f"{what}: operation {op} never terminal")
    if status != "Succeeded":
        raise SystemExit(f"{what}: operation {op} {status}: {got}")
    _, _, res = must("GET", f"{FABRIC}/v1/operations/{op}/result", headers=bearer(FABRIC_AUD))
    return res


def find_workspace(name: str):
    for w in fabric_get("/v1/workspaces").get("value", []):
        if w["displayName"] == name:
            return w
    return None


def find_item(ws_id: str, name: str, item_type: str):
    for it in fabric_get(f"/v1/workspaces/{ws_id}/items?type={item_type}").get("value", []):
        if it["displayName"] == name:
            return it
    return None


def odbc_server(connection_string: str) -> str:
    """`host:port` -> `host,port` for ODBC; bare FQDN passes through."""
    host, sep, port = connection_string.rpartition(":")
    return f"{host},{port}" if sep and port.isdigit() else connection_string


def sql_endpoint(ws_id: str, wh_id: str, tds_server_override: str = "") -> tuple[str, str]:
    """(server, database) for a Warehouse, from its documented properties.

    The database is addressed by DISPLAY NAME and the server is the advertised
    `properties.connectionString`, as Fabric does. `tds_server_override` exists
    only for a stack whose advertised address is not dialable from here (a
    Docker port remap); it is config, never set in prod.
    """
    wh = fabric_get(f"/v1/workspaces/{ws_id}/warehouses/{wh_id}")
    server = tds_server_override or (wh.get("properties") or {}).get("connectionString")
    if not server:
        raise SystemExit("warehouse advertises no connectionString")
    return odbc_server(server), wh["displayName"]


def tds_connect(server: str, database: str, access_token: str | None = None, timeout=60):
    """TDS with an Entra access token (SQL_COPT_SS_ACCESS_TOKEN, attr 1256):
    4-byte length + UTF-16-LE token. mssql-python bundles Microsoft's driver."""
    import mssql_python  # imported lazily so --help works without the driver

    enc = (access_token or token(SQL_AUD)).encode("utf-16-le")
    encrypt = CFG.get("DAS_TDS_ENCRYPT", "no")
    return mssql_python.connect(
        f"Server={server};Database={database};Encrypt={encrypt};TrustServerCertificate=yes",
        attrs_before={1256: struct.pack("<i", len(enc)) + enc},
        timeout=timeout,
    )


def sources() -> list[dict]:
    return json.loads(CFG.get("DAS_SOURCES", "[]"))


def source_by_name(name: str) -> dict:
    for src in sources():
        if src.get("name") == name:
            return src
    raise SystemExit(f"no source named {name} in DAS_SOURCES")


def connect_source(src: dict):
    """A DB-API connection to whichever engine a source names.

    The harnesses need to reach a source directly — to seed it, and to run an
    eval's reference query so the oracle is the data rather than a number typed
    into a fixture. That has to work per engine, because the second source is
    the whole point of having a second source.

    The SERVICE never uses this: it connects through its own adapters, as the
    asking user. This is the harness's own door, and it uses whatever
    credential the local seed has.
    """
    kind = src.get("kind", "fabric")
    if kind in ("fabric", "azuresql", "synapse"):
        server = src.get("tds_server") or ""
        database = src.get("database") or src.get("item") or ""
        if not server:
            state = load_state()
            server, database = state.get("sql_server", ""), state.get("sql_database", "")
        # A source advertises `host:port`; the driver wants `host,port`. The
        # conversion lives in one place so a second caller cannot forget it.
        return tds_connect(odbc_server(server), database)
    if kind == "postgres":
        import psycopg

        return psycopg.connect(**postgres_connect_args(src), connect_timeout=15)
    raise SystemExit(f"no harness connection for source kind {kind!r}")


def postgres_connect_args(src: dict) -> dict:
    """How to reach a PostgreSQL source: its DSN, and the password from
    wherever that source keeps it.

    The executor makes the same decision (`services/warehouse-query-py/
    sources.py`), and the seed has to agree with it or `make seed` succeeds
    against a database the running service cannot open. A source with a
    `credential` keeps its password in the vault and NOT in its DSN, so this
    is the only place the seed learns it.
    """
    dsn = src.get("dsn") or ""
    if not dsn:
        raise SystemExit(f"source {src.get('name')} has no dsn")
    args: dict = {"conninfo": dsn}
    credential = (src.get("credential") or "").strip()
    if credential:
        import vaultref

        args["password"] = vaultref.resolve(credential)
    return args


def source_for(workspace: str, item: str) -> dict:
    for src in sources():
        if src.get("workspace") == workspace and src.get("item") == item:
            return src
    return {}


# ------------------------------------------------------------------ state --
def load_state() -> dict:
    return json.loads(STATE.read_text()) if STATE.exists() else {}


# Settings whose NAME contains a secret-ish word but whose VALUE is not one.
# Written out rather than pattern-matched around: each is a deliberate
# judgement, and a list you can read is the only kind anyone re-checks.
NOT_SECRETS = frozenset(
    {
        "AZURE_TOKEN_CREDENTIALS",  # which credential TYPE to use
        "DAS_EXECUTOR_SECRET_NAME",  # the name of a secret, which is the point
        "DAS_KEYVAULT_URL",  # a URL that happens to contain "_KEY"
        "DAS_LLM_TOKENS_PER_MINUTE",  # a quota
        "DAS_LLM_CALLS_PER_MINUTE",  # a quota
        "DAS_PROMOTE_KEY_SECRET",  # named for the key it derives, set by hand
    }
)


def looks_like_a_secret(name: str) -> bool:
    """Would a value under this name be a credential?

    Substring matching with an exemption list, not an exact list of known
    secrets: a setting nobody has added yet should be caught by default, and
    the cost of that is naming the exceptions.
    """
    if name in NOT_SECRETS:
        return False
    return any(word in name.upper() for word in ("SECRET", "PASSWORD", "_KEY", "TOKEN"))


def setting(name: str, default: str = "") -> str:
    """A configured value, with `keyvault:` references resolved.

    Every harness reads its settings through here rather than off CFG
    directly, so a reference works wherever a literal did. See vaultref.py for
    why the reference exists at all.
    """
    import vaultref

    return vaultref.resolve(CFG.get(name, default))


def store_secret(name: str, value: str) -> None:
    """Put a secret in Key Vault and return nothing.

    Nothing, deliberately: a caller that wanted the value back would be about
    to write it somewhere, and where it gets written is the thing this exists
    to stop.
    """
    kv = CFG.get("DAS_KEYVAULT_URL", "").rstrip("/")
    if not kv:
        log(f"secret {name}: no DAS_KEYVAULT_URL, skipped")
        return
    st, _hd, body = http(
        "PUT",
        f"{kv}/secrets/{name}?api-version=7.5",
        headers=bearer("https://vault.azure.net"),
        json_body={"value": value},
    )
    if st not in (200, 201):
        raise SystemExit(f"key vault PUT {name}: {st} {body[:300]}")
    log(f"secret {name}: stored in Key Vault")


def write_env(**values: str) -> None:
    """Persist ids the seed CREATED back into the env file this run loaded.

    Seeding brings things into existence whose identifiers the service must then
    be configured with — the API app registration, the gateway's subscription
    key. Without this they live only in state.json, so a fresh clone seeds
    successfully and still cannot start: the executor reads its configuration
    from the environment, not from the seed's notes.

    Production is never rewritten. `.env.prod` is authored from the runbook,
    where the app registrations are made with `az ad` by someone who decides
    their names; a seed reaching in and editing that file would be surprising in
    exactly the place surprises are expensive.
    """
    # Refused BEFORE anything else, including the checks that return early.
    # This guard used to sit next to the write, after `if not target.exists():
    # return` -- so on a machine with no .env it never ran, and the test for it
    # passed locally and failed in CI. A caller passing a credential is a
    # programming error whether or not there is a file to write it to.
    for key, value in values.items():
        if looks_like_a_secret(key) and value and not value.startswith("keyvault:"):
            raise SystemExit(
                f"write_env({key}=…) would put a secret in the settings file in clear "
                f"text. Store it with store_secret() and write keyvault:<name> instead."
            )

    if os.environ.get("DAS_ENV", "local") == "prod":
        log("prod environment: not rewriting .env.prod (see docs/10-production.md)")
        return
    target = ROOT / ".env"
    if not target.exists():
        return
    lines = target.read_text().splitlines()
    remaining = dict(values)
    for i, line in enumerate(lines):
        key = line.split("=", 1)[0].strip()
        if key in remaining:
            lines[i] = f"{key}={remaining.pop(key)}"
    for key, value in remaining.items():
        lines.append(f"{key}={value}")
    target.write_text("\n".join(lines) + "\n")
    # This file holds a client secret and a gateway subscription key. It is
    # gitignored, and on a shared machine that is not the same as private --
    # anyone with an account could read it at the default umask.
    target.chmod(0o600)
    for key, value in values.items():
        CFG[key] = value
    # `.keys()` is explicit on purpose. Joining the dict itself yields the same
    # string, but it reads as though the secrets were being printed, and a
    # static analyser cannot tell the difference from a reader who guesses.
    log(f"wrote {', '.join(sorted(values.keys()))} to .env")


def save_state(**kv) -> dict:
    st = load_state()
    st.update(kv)
    STATE.write_text(json.dumps(st, indent=2))
    return st


if __name__ == "__main__":
    print(
        json.dumps({k: v for k, v in CFG.items() if "SECRET" not in k and "KEY" not in k}, indent=1)
    )
    sys.exit(0)
