#!/bin/sh
# Rule 2, checked rather than promised: no emulator-only code paths.
#
# The claim this repo makes is that pointing at real Azure is a settings change.
# That claim decays quietly — one `if emulator:`, one convenient admin endpoint
# that production does not serve, one hardcoded localhost — and each of those
# still passes every test, because the tests run against the emulators.
#
# So the shape is checked directly. Two questions:
#
#   1. Does anything BRANCH on which target it is talking to?
#   2. Does anything call a surface that exists only in a development stack?
#
# Scope: the code that runs in production or that must work identically against
# both targets. Not `docs/` (which describes both), not `.env`/compose (which
# is where the difference is SUPPOSED to live), not this file.
#
# Exit 0 = clean, 1 = at least one violation.
set -eu

RC=0
SCOPE="agent evals e2e load seed services tests"

report() {  # what, why, matches
  printf '\033[31mFAIL\033[0m  %s\n' "$1"
  printf '      %s\n' "$2"
  printf '%s\n' "$3" | sed 's/^/        /'
  RC=1
}

ok() { printf '\033[32mok\033[0m    %s\n' "$1"; }

# 1. Development-only surfaces. Each of these exists in the emulator family and
#    has no counterpart in Azure, so a call to one cannot run in production.
FORBIDDEN='/admin/api/tokens|/admin/api/faults|/_emulator/|APIM_DISABLE_AUTH|FABRIC_FORCE_LRO|/admin/api/clock'
hits=$(grep -rnE "$FORBIDDEN" $SCOPE --include='*.py' --include='*.go' --include='*.js' --include='*.mjs' \
        2>/dev/null | grep -v 'check-discipline' || true)
if [ -n "$hits" ]; then
  report "a development-only surface is called" \
         "these exist in the emulator family and not in Azure" "$hits"
else
  ok "no development-only surface is called"
fi

# 1b. The tenant's own administrative surface. The emulator serves one; Entra
#     does not, so a call to it cannot run in production. Two of Graph's writes
#     are not honoured by the development tenant (docs/upstream-issues.md #5,
#     #9), and the seeds fall back to it for SETUP ONLY — never at request time,
#     and only where the postcondition is already true against a real tenant.
#     Each such call must say so on the line or just above it, so the exceptions
#     stay countable instead of accumulating.
admin=$(grep -rn '/admin/api' $SCOPE --include='*.py' --include='*.go' 2>/dev/null || true)
unmarked=""
if [ -n "$admin" ]; then
  for hit in $(printf '%s\n' "$admin" | cut -d: -f1,2 | tr ' ' '~'); do
    file=$(printf '%s' "$hit" | cut -d: -f1)
    line=$(printf '%s' "$hit" | cut -d: -f2)
    # Six lines, not three: the formatter splits a call across several lines,
    # so the marker above it can be further from the URL than it looks in
    # source. Still tight enough that the marker must be adjacent to the call.
    start=$(( line - 6 )); [ $start -lt 1 ] && start=1
    if ! sed -n "${start},${line}p" "$file" | grep -q 'emulator-setup-only'; then
      unmarked="$unmarked$file:$line\n"
    fi
  done
fi
if [ -n "$unmarked" ]; then
  report "a tenant administrative surface is called without justification" \
         "mark setup-only fallbacks with 'emulator-setup-only', or remove them" \
         "$(printf '%b' "$unmarked")"
else
  count=$(printf '%s\n' "$admin" | grep -c '/admin/api' || true)
  ok "administrative-surface calls are setup-only and marked ($count)"
fi

# 2. Branching on the target. `DAS_ENTRA_TLS_INSECURE` is the family's
#    self-signed-certificate switch and is configuration, not a branch on
#    identity — it is allowed, and it is off in production.
BRANCHES='is_emulator|IS_EMULATOR|if.*emulator|target *== *.emulator|FABRIC_TARGET'
hits=$(grep -rnE "$BRANCHES" $SCOPE --include='*.py' --include='*.go' --include='*.js' --include='*.mjs' \
        2>/dev/null | grep -viE 'entra-emulator|apim-emulator|fabric-emulator|keyvault-emulator|arm-emulator|# |// ' || true)
if [ -n "$hits" ]; then
  report "code branches on whether the target is an emulator" \
         "the target must be a setting, never a code path" "$hits"
else
  ok "nothing branches on whether the target is an emulator"
fi

# 3. Hardcoded endpoints in code. Addresses belong in configuration; one baked
#    into a module is the thing that cannot be re-pointed.
#
#    Test files are exempt, and only here: a fixture naming `127.0.0.1:1` to
#    simulate a directory that will not answer is stating the scenario, not
#    choosing a deployment. They stay in scope for every other check — a test
#    calling an emulator-only surface would still be a finding, because it
#    would mean the behaviour is only reachable one way.
HARDCODED='https?://(localhost|127\.0\.0\.1|[a-z-]+-emulator)'
# The exact literal `"redirectUris": ["http://localhost"]` is exempt, and
# nothing looser: matching the KEY alone would exempt the whole line, so a real
# endpoint sitting beside it would ride through. Verified with a canary. A public client's loopback
# redirect is RFC 8252 §7.3: `http://localhost` is what Entra registers for a
# native or desktop client IN PRODUCTION too, byte for byte. It is a protocol
# constant rather than a deployment address, so it does not become wrong when
# the target stops being an emulator -- which is the only thing this rule is
# for. Nothing else about a redirect URI is exempted, and the emulator
# hostnames still are not.
# A comment is `path:line: #…` or `path:line: //…` — matching a bare "//"
# would exclude every URL, which is every line this check exists to find.
hits=$(grep -rnE "$HARDCODED" $SCOPE --include='*.py' --include='*.go' \
        2>/dev/null \
        | grep -vE '(^tests/|_test\.go:|/test_[a-z_]*\.py:|^[^:]*/tests?/)' \
        | grep -vE '^[^:]*:[0-9]+: *(#|//|\*)' \
        | grep -vE '(getenv|environ\.get|os\.Getenv|CFG\.get)\(' \
        | grep -vE '"redirectUris": \["http://localhost"\]' || true)
if [ -n "$hits" ]; then
  report "an endpoint is hardcoded in code" \
         "addresses belong in .env; a default beside a getenv is fine" "$hits"
else
  ok "no endpoint is hardcoded outside a configuration default"
fi

# 4. Credentials in code. The seeded emulator passwords are real strings that
#    would silently become production credentials if one were ever pasted.
SECRETS='daemon-app-secret|Password1!|Str0ng!Passw0rd|managed-identity-secret'
# Allowed only as the default ARGUMENT of a configuration read — a literal
# anywhere else is a credential in code, however it is spelled.
hits=$(grep -rnE "$SECRETS" $SCOPE --include='*.py' --include='*.go' \
        2>/dev/null \
        | grep -vE '^[^:]*:[0-9]+: *(#|//|\*)' \
        | grep -vE '(getenv|environ\.get|os\.Getenv|CFG\.get)\([^)]*,' || true)
if [ -n "$hits" ]; then
  report "a development credential is written into code" \
         "credentials come from configuration or Key Vault, with no literal fallback" "$hits"
else
  ok "no development credential is written into code"
fi

# 5. The production environment file must not describe a development stack.
if [ -f .env.prod ]; then
  bad=$(grep -nE '(localhost|127\.0\.0\.1|-emulator|TLS_INSECURE=true|Password1!|daemon-app-secret)' \
        .env.prod || true)
  if [ -n "$bad" ]; then
    report ".env.prod still points at a development stack" \
           "ENV=prod must reach real Azure" "$bad"
  else
    ok ".env.prod points at real endpoints"
  fi
else
  ok ".env.prod absent (nothing to check)"
fi

[ $RC -eq 0 ] && printf '\ndiscipline: clean\n' || printf '\ndiscipline: violations above\n'
exit $RC
