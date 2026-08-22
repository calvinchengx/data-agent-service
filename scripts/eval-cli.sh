#!/usr/bin/env bash
# Run the evals through the `claude` CLI instead of the Anthropic SDK.
#
# The SDK backend needs an API key. A Claude subscription is a different
# credential, and `claude` already holds it — so this path lets a machine with
# Claude Code score itself with no key at all. It measures Claude Code's loop
# over our MCP servers rather than our own loop; docs/07-evaluation.md says
# what that difference means.
#
# Three things have to be arranged because the CLI runs on the HOST while the
# tenant and the databases live inside the compose network:
#
#   1. the persona cannot sign in from here, so a token is minted inside the
#      network and handed over (DAS_HARNESS_AUTH=token);
#   2. the scorer opens each source directly to compare result sets, so source
#      addresses are rewritten to ones the host can reach;
#   3. `localhost` is NOT one of them when a host-local server already holds
#      the port — a loopback bind beats docker's wildcard publish, so a local
#      PostgreSQL silently wins and the scorer connects to the wrong database.
#      Container addresses avoid the question entirely.
set -euo pipefail
cd "$(dirname "$0")/.."

# Every persona the suite can ask as, not only the default one: an L5 question
# names its own persona, and a token minted for one caller says nothing about
# another. Missing one fails the run halfway through rather than at the start.
PERSONAS="${DAS_EVAL_PERSONAS:-carol@entraemulator.dev alice@entraemulator.dev bob@entraemulator.dev}"
USER_UPN="${DAS_EVAL_USER:-carol@entraemulator.dev}"

command -v claude >/dev/null || { echo "the \`claude\` CLI is not on PATH"; exit 1; }
command -v uv >/dev/null || { echo "uv is required to run the harness on the host"; exit 1; }

# One container invocation for all of them: each is several seconds.
MINTED=$(docker compose --profile tools run --rm -T tools python -c "
from agent import identity
for upn in '$PERSONAS'.split():
    print(identity.env_key(upn) + '=' + identity.token_for(upn))
" 2>/dev/null | tr -d '\r' | grep '^DAS_TOKEN_')
[ -n "$MINTED" ] || { echo "could not mint tokens — is the stack up?"; exit 1; }

export DAS_HARNESS_AUTH=token
while IFS= read -r line; do export "${line?}"; done <<< "$MINTED"
echo "tokens minted: $(echo "$MINTED" | wc -l | tr -d ' ') personas"
export DAS_SOURCES
DAS_SOURCES=$(python3 scripts/host_sources.py)

echo "evaluating as $USER_UPN through the claude CLI"
exec uv run python -m evals.runner --agent claude-code "$@"
