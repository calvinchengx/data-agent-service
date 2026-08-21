#!/bin/sh
# Is the stack usable? Probes each dependency on its host port. Exit non-zero
# if any required service is down.
set -u
. ./.env 2>/dev/null || true
RC=0
ok()  { printf '  ok    %-14s %s\n' "$1" "$2"; }
bad() { printf '  DOWN  %-14s %s\n' "$1" "$2"; RC=1; }

probe() { # name url expect-substring
  body=$(curl -sk --max-time 5 "$2" 2>/dev/null || true)
  if printf '%s' "$body" | grep -q "$3"; then ok "$1" "$2"; else bad "$1" "$2"; fi
}

T=6f89cf12-978b-4d23-ac18-9ef0c127cf87
probe entra     "https://localhost:${ENTRA_PORT:-8443}/$T/v2.0/.well-known/openid-configuration" '"issuer"'
probe keyvault  "https://localhost:${KEYVAULT_PORT:-8444}/secrets?api-version=7.5" 'Bearer'
probe arm       "http://localhost:${ARM_PORT:-8445}/health" '"ok"'
probe fabric    "https://localhost:${FABRIC_PORT:-9443}/v1/workspaces" '.'
probe apim      "https://localhost:${APIM_PORT:-8446}/health" '"ok"'
probe openmetadata "http://localhost:${OM_PORT:-8585}/api/v1/system/version" '"version"'

if command -v nc >/dev/null 2>&1; then
  if nc -z localhost "${FABRIC_TDS_PORT:-1433}" 2>/dev/null; then ok "warehouse-tds" "localhost:${FABRIC_TDS_PORT:-1433}"; else bad "warehouse-tds" "localhost:${FABRIC_TDS_PORT:-1433}"; fi
fi

[ $RC -eq 0 ] && printf '\nstack OK\n' || printf '\nstack NOT ready\n'
exit $RC
