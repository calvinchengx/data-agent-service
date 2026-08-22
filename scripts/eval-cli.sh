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

# The gateway answers on a PUBLISHED port here, not on its compose hostname,
# and the published port is chosen by compose rather than by us -- 8445 is
# taken on this host, so the gateway lands on 8446. Asking docker keeps this
# correct when that changes; a literal would be right until the day it wasn't.
GW_ADDR=$(docker compose port apim-emulator 8445 2>/dev/null | tail -1)
if [ -n "$GW_ADDR" ]; then
  export DAS_CLAUDE_APIM_BASE="https://localhost:${GW_ADDR##*:}"
  echo "gateway (published): $DAS_CLAUDE_APIM_BASE"
fi

# The catalog's gateway key is a `keyvault:` reference, and Key Vault is
# inside the network too -- so it is resolved there and handed over as a
# literal, exactly like the tokens. Resolving on the host fails twice: no
# DAS_KEYVAULT_URL in this process, and no route to the vault if there were.
OM_KEY=$(docker compose --profile tools run --rm -T tools python -c "
from seed import common as c
print(c.setting('DAS_OM_SUBSCRIPTION_KEY'))" 2>/dev/null | tr -d '\r' | tail -1)
[ -n "$OM_KEY" ] && export DAS_OM_SUBSCRIPTION_KEY="$OM_KEY"

export DAS_HARNESS_AUTH=token
while IFS= read -r line; do export "${line?}"; done <<< "$MINTED"
echo "tokens minted: $(echo "$MINTED" | wc -l | tr -d ' ') personas"
export DAS_SOURCES
DAS_SOURCES=$(python3 scripts/host_sources.py)

# The scorer signs in to each source to run the reference SQL. A source whose
# sign-in goes through a name only the compose network resolves cannot be
# scored from here, and saying so now is better than a traceback thirty lines
# deep once the model has already been asked.
# The scorer signs in to each source to run the reference SQL. A source whose
# sign-in goes through a name only the compose network resolves cannot be
# scored from here, and saying so now is better than a traceback thirty lines
# deep once the model has already been asked.
#
# Scoped to the sources THIS run needs, read from the questions themselves. An
# earlier version asked whether any CONFIGURED source was unreachable, which
# refused a PostgreSQL-only use case because a Fabric source existed in the
# same file — a preflight that blocks a run it has no reason to block.
if ! DAS_EVAL_USECASE="${DAS_EVAL_USECASE:-}" uv run python - "$@" <<'PREFLIGHT'
import json, pathlib, socket, sys, urllib.parse
sys.path.insert(0, ".")
from seed import common as c

argv = sys.argv[1:]
usecase = "contoso"
for i, arg in enumerate(argv):
    if arg == "--usecase" and i + 1 < len(argv):
        usecase = argv[i + 1]
    elif arg.startswith("--usecase="):
        usecase = arg.split("=", 1)[1]

questions = pathlib.Path("evals/usecases") / usecase / "questions.jsonl"
default = c.CFG.get("DAS_DEFAULT_SOURCE", "")
needed = set()
for line in questions.read_text().splitlines():
    if line.strip():
        needed.add(json.loads(line).get("source") or default)

kinds = {
    s["name"]: s.get("kind", "fabric")
    for s in json.loads(c.CFG.get("DAS_SOURCES", "[]"))
}
issuer = urllib.parse.urlsplit(c.CFG["DAS_ENTRA_ISSUER"]).hostname or ""
try:
    socket.gethostbyname(issuer)
except OSError:
    unreachable = sorted(n for n in needed if kinds.get(n, "fabric") != "postgres")
    if unreachable:
        print(f"cannot score {usecase} from the host: it needs {', '.join(unreachable)},")
        print(f"which signs in through the tenant ({issuer}) for the reference SQL, and that")
        print("name is not resolvable here. Run the in-container path instead (needs an API key):")
        print(f'  make eval ARGS="--usecase {usecase} --tier L3 --ablation"')
        sys.exit(1)
PREFLIGHT
then exit 1; fi

echo "evaluating as $USER_UPN through the claude CLI"
exec uv run python -m evals.runner --agent claude-code "$@"
