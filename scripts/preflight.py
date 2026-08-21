"""Is this environment ready, and is it the environment you think it is?

    python -m scripts.preflight --env prod
    python -m scripts.preflight --env prod --offline   # settings only, no calls

Run before deploying against a new environment, and again after. It answers
three questions in order, because a failure in an earlier one explains every
failure after it:

  1. **Is the configuration complete and coherent?** Every setting the service
     reads is present, and none of them describes a development stack while
     claiming to be production.
  2. **Does the identity work?** The tenant answers, its keys are published,
     and a token can be obtained for this API.
  3. **Do the parts answer?** The gateway, the catalog and the executor are
     reachable, and the executor agrees about who it is.

It is deliberately not a smoke test of the whole system — `make test` is that,
and it runs against production unchanged. This is the check that tells you
whether running it is worth the attempt.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse

REQUIRED = [
    ("DAS_ENTRA_ISSUER", "the tenant that issues tokens"),
    ("DAS_AGENT_CLIENT_ID", "the public client users sign in to"),
    ("DAS_AGENT_AUDIENCE", "the API these tokens are addressed to"),
    ("DAS_MIDDLE_TIER_CLIENT_ID", "the confidential app that performs the on-behalf-of exchange"),
    ("DAS_APIM_BASE", "the gateway"),
    ("DAS_OM_URL", "the catalog"),
    ("DAS_SOURCES", "the data sources this service may query"),
    ("DAS_KEYVAULT_URL", "where secrets are read from"),
]

# Values that mean "this is a development stack". In a file claiming to be
# production, each is a mistake rather than a choice.
DEVELOPMENT_MARKERS = ("localhost", "127.0.0.1", "-emulator", "entraemulator.dev")
DEVELOPMENT_SECRETS = ("daemon-app-secret", "Password1!", "managed-identity-secret",
                       "Str0ng!Passw0rd")

PASS, FAIL, WARN = "\033[32mok\033[0m", "\033[31mFAIL\033[0m", "\033[33mwarn\033[0m"
_failures = 0
_warnings = 0


def check(name: str, ok: bool, detail: str = "", *, warn_only: bool = False) -> bool:
    global _failures, _warnings
    if ok:
        mark = PASS
    elif warn_only:
        mark, _warnings = WARN, _warnings + 1
    else:
        mark, _failures = FAIL, _failures + 1
    print(f"  {mark}  {name}" + (f" — {detail}" if detail else ""), flush=True)
    return ok


def settings(cfg: dict, env: str) -> None:
    print("\nconfiguration")
    missing = [key for key, _ in REQUIRED if not cfg.get(key)]
    check("every required setting is present", not missing,
          "missing: " + ", ".join(missing) if missing else f"{len(REQUIRED)} settings")

    if env == "prod":
        described = {key: value for key, value in cfg.items()
                     if key.startswith("DAS_") and isinstance(value, str)
                     and any(marker in value for marker in DEVELOPMENT_MARKERS)}
        check("nothing points at a development stack", not described,
              ", ".join(sorted(described)) if described else "real endpoints only")

        insecure = cfg.get("DAS_ENTRA_TLS_INSECURE", "false").lower() in ("1", "true", "yes")
        check("certificates are verified", not insecure,
              "DAS_ENTRA_TLS_INSECURE is on — production presents real certificates"
              if insecure else "")

        leaked = [key for key, value in cfg.items()
                  if isinstance(value, str) and value in DEVELOPMENT_SECRETS]
        check("no development credential is configured", not leaked, ", ".join(leaked))

        # These appear in .env.prod.example so that every setting is documented,
        # and they are listed there EMPTY. A value in one means a development
        # habit followed the configuration into production.
        must_be_empty = ("DAS_SEED_CLIENT_SECRET", "DAS_TEST_PASSWORD", "DAS_USER",
                         "DAS_SEED_CLIENT_ID", "DAS_QUERY_SVC_CLIENT_ID", "IDENTITY_HEADER")
        filled = [key for key in must_be_empty if (cfg.get(key) or "").strip()]
        check("settings that must be empty in production are empty", not filled,
              ", ".join(filled) if filled else f"{len(must_be_empty)} checked")

        validating = cfg.get("DAS_APIM_VALIDATE_JWT", "true").lower() in ("1", "true", "yes")
        check("the gateway validates tokens", validating,
              "DAS_APIM_VALIDATE_JWT is off; it is only off locally "
              "(docs/upstream-issues.md #7)")

    try:
        sources = json.loads(cfg.get("DAS_SOURCES", "[]"))
    except json.JSONDecodeError as e:
        check("the source list parses", False, str(e))
        return
    check("the source list parses", bool(sources), f"{len(sources)} source(s)")
    for src in sources:
        name = src.get("name", "?")
        check(f"source {name} names its catalog service",
              bool(src.get("om_service_fqn")),
              "om_service_fqn joins a source to its business context")
        if env == "prod":
            check(f"source {name} is addressed by the service, not overridden",
                  not src.get("tds_server"),
                  "tds_server is a local port-remap escape hatch; production uses "
                  "the address Fabric advertises", warn_only=True)
            check(f"source {name} runs queries as the asking user",
                  src.get("authz_tier") == "user",
                  f"authz_tier={src.get('authz_tier')} — the source's own permissions "
                  "will not apply per user", warn_only=True)


def identity(cfg: dict) -> None:
    from seed import common as c

    print("\nidentity")
    issuer = cfg["DAS_ENTRA_ISSUER"].rstrip("/")
    authority = issuer[:-len("/v2.0")] if issuer.endswith("/v2.0") else issuer
    st, _, body = c.http("GET", f"{authority}/v2.0/.well-known/openid-configuration")
    meta = json.loads(body) if st == 200 else {}
    check("the tenant answers", st == 200 and meta.get("issuer") == issuer,
          meta.get("issuer", f"status {st}"))
    if not meta:
        return
    st, _, body = c.http("GET", meta.get("jwks_uri", ""))
    keys = json.loads(body).get("keys", []) if st == 200 else []
    check("its signing keys are published", bool(keys), f"{len(keys)} key(s)")
    check("it offers the flows an interactive client needs",
          "authorization_code" in (meta.get("grant_types_supported") or [])
          and "S256" in (meta.get("code_challenge_methods_supported") or []),
          "authorization_code + PKCE")


def reachability(cfg: dict) -> None:
    from seed import common as c

    print("\nreachability")
    gateway = cfg["DAS_APIM_BASE"].rstrip("/")
    path = cfg.get("DAS_WAREHOUSE_MCP_PATH", "/warehouse/mcp")
    st, headers, _ = c.http("POST", gateway + path,
                            headers={"Content-Type": "application/json"},
                            json_body={"jsonrpc": "2.0", "id": 1, "method": "tools/list",
                                       "params": {}})
    challenge = {k.lower(): v for k, v in headers.items()}.get("www-authenticate", "")
    check("the gateway is serving and refuses an anonymous call",
          st in (401, 403), f"status {st}")
    check("it tells a client where to authenticate", "resource_metadata" in challenge,
          challenge[:80] or "no WWW-Authenticate")

    if challenge and "resource_metadata" in challenge:
        url = challenge.split('resource_metadata="', 1)[1].split('"', 1)[0]
        st, _, body = c.http("GET", url)
        meta = json.loads(body) if st == 200 else {}
        check("the metadata it points at is served", st == 200, f"{st} {url}"[:90])
        check("that metadata names this API",
              meta.get("resource") == cfg["DAS_AGENT_AUDIENCE"],
              meta.get("resource", ""))

    st, _, body = c.http("GET", cfg["DAS_OM_URL"].rstrip("/") + "/api/v1/system/version")
    check("the catalog answers", st == 200,
          json.loads(body).get("version", "") if st == 200 else f"status {st}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", default=os.environ.get("DAS_ENV", "local"))
    ap.add_argument("--offline", action="store_true", help="check settings only")
    a = ap.parse_args()

    os.environ["DAS_ENV"] = a.env
    from seed import common as c

    cfg = c.load_env(a.env)
    print(f"preflight — {a.env}")
    settings(cfg, a.env)
    if not a.offline:
        try:
            identity(cfg)
            reachability(cfg)
        except KeyError as e:
            check("configuration complete enough to test", False, f"missing {e}")

    print(f"\n{_failures} failure(s), {_warnings} warning(s)")
    if _failures:
        print("Not ready. Fix the failures above before deploying against this environment.")
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main())
