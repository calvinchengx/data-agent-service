#!/bin/sh
# Can this machine run `make up`? Exit 0 = ready, 1 = at least one blocker.
set -eu
RC=0
ok()   { printf '  ok    %-14s %s\n' "$1" "$2"; }
warn() { printf '  warn  %-14s %s\n' "$1" "$2"; }
bad()  { printf '  FAIL  %-14s %s\n' "$1" "$2"; RC=1; }

printf 'shell tools\n'
for t in sh grep awk curl docker; do
  p=$(command -v "$t" 2>/dev/null || true)
  if [ -n "$p" ]; then ok "$t" "$p"; else bad "$t" "not on PATH"; fi
done

printf '\npython (harnesses, seeds, agent)\n'
PY=""
for c in python3.13 python3.12 python3 python py; do
  if "$c" -c 'import sys; assert sys.version_info >= (3,12)' >/dev/null 2>&1; then PY="$c"; break; fi
done
if [ -n "$PY" ]; then ok "$PY" "$("$PY" -c 'import sys; print(sys.version.split()[0])')"; else bad "python" "need 3.12+"; fi

printf '\ndocker\n'
if docker info >/dev/null 2>&1; then
  ok "daemon" "$(docker context show 2>/dev/null | head -n1)"
  mem=$(docker info --format '{{.MemTotal}}' 2>/dev/null || echo 0)
  gb=$((mem / 1073741824))
  if [ "$gb" -ge 14 ]; then ok "memory" "${gb} GB available to the engine"
  else warn "memory" "${gb} GB available; the full stack wants ~14 GB"; fi
  if docker compose version >/dev/null 2>&1; then ok "compose" "$(docker compose version --short)"; else bad "compose" "docker compose v2 not found"; fi
else
  bad "daemon" "docker daemon not reachable"
fi

printf '\nconfig\n'
if [ -f .env ]; then ok ".env" "present"; else warn ".env" "missing; make up copies .env.example"; fi
if grep -q '^ANTHROPIC_API_KEY=.\+' .env 2>/dev/null; then ok "ANTHROPIC_API_KEY" "set"; else warn "ANTHROPIC_API_KEY" "unset; only the agent (Phase 7) needs it"; fi

exit $RC
