#!/usr/bin/env bash
# Mint one persona token, from inside the compose network.
#
# Called by the harness through DAS_TOKEN_REFRESH_CMD when a supplied token has
# expired. It exists because the eval driver runs on the HOST, where the tenant
# is not resolvable -- the same reason the initial tokens are minted inside and
# handed over. A production run reaches the tenant directly and never uses this.
set -euo pipefail
cd "$(dirname "$0")/.."
docker compose --env-file .env --profile tools run --rm -T tools python -c "
import sys
from agent import identity
print(identity.token_for(sys.argv[1]))
" "$1" 2>/dev/null | tr -d '\r' | grep -E '^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.' | tail -1
