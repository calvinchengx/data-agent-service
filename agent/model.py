"""The model, as one shape the agent can talk to whatever is behind it.

WHY THIS IS NOT A THIN WRAPPER OVER ONE `create()` CALL. The protocols shape
CONVERSATIONS differently, not just requests. Anthropic carries tool results
as blocks inside a user message and marks a rolling cache breakpoint on the
newest one; OpenAI's chat completions carries each result as its own
`role: "tool"` message and has no cache concept at all. A seam drawn at the
request would leak one protocol's message list into the loop. So the backend
owns the transcript, and the agent sees only turns.

WHY CAPABILITIES ARE DECLARED RATHER THAN DISCOVERED. Being able to reach any
gateway is worth something; pretending every gateway is equivalent is worth
less than nothing. Prompt caching is the clearest case -- the system prompt is
deliberately split into two cached blocks so the shared half caches across
callers, and a backend without caching silently pays for that prefix on every
hop of every question. The deployment should see that in its own telemetry
rather than on an invoice.

So a backend says what it can do, `require()` decides whether that is enough,
and what is missing travels on every `Hop`. The rule is the one this
repository already applies to `authz_tier`: a weaker tier should LOOK weaker.

Required capabilities are refused outright. A model that cannot call tools
cannot serve this product at all -- the whole service is a tool loop -- and
refusing at construction is the same choice `load_sources` makes for a source
claiming an identity its engine cannot provide.
"""

from __future__ import annotations

import dataclasses
from typing import Any, Protocol, runtime_checkable

# ------------------------------------------------------------ capabilities --

#: The model can be given tools and can ask for them. Without it there is no
#: product, so a backend lacking it is refused rather than degraded.
TOOL_USE = "tool_use"

#: Cache breakpoints on the system prompt and the newest tool result.
PROMPT_CACHING = "prompt_caching"
#: The response says how many tokens were read from and written to that cache.
#: Separate from PROMPT_CACHING: a backend can cache and not report it, and
#: then the saving is real but invisible, which is its own problem.
CACHE_USAGE = "cache_usage"
#: A reasoning-effort control.
EFFORT = "effort"
#: The provider can route around its own refusals rather than failing the run.
SERVER_FALLBACK = "server_fallback"
#: Refusal and pause are distinguishable stop reasons rather than one error.
REFUSAL = "refusal"

REQUIRED = frozenset({TOOL_USE})
WANTED = frozenset({PROMPT_CACHING, CACHE_USAGE, EFFORT, SERVER_FALLBACK, REFUSAL})


class Unsupported(Exception):
    """A backend cannot do something this service is not willing to go without."""


def require(capabilities: frozenset[str], *, accept_degraded: bool) -> tuple[str, ...]:
    """What this backend gives up, having refused what it may not give up.

    Missing a REQUIRED capability is always fatal. Missing a WANTED one is
    fatal unless the operator has said, in configuration, that they accept it
    -- because the cost of running degraded is real, is theirs, and should be
    a decision rather than a discovery.
    """
    missing = REQUIRED - capabilities
    if missing:
        raise Unsupported(
            f"this model backend cannot {', '.join(sorted(missing))}, which this service "
            "cannot work without: every answer here is produced by calling tools."
        )
    degraded = tuple(sorted(WANTED - capabilities))
    if degraded and not accept_degraded:
        raise Unsupported(
            f"this model backend does without {', '.join(degraded)}. That is supported and "
            "it costs you: without prompt_caching the shared system prompt is paid for on "
            "every hop of every question. Set DAS_LLM_ACCEPT_DEGRADED=true to run this way "
            "on purpose (docs/09-llm-governance.md)."
        )
    return degraded


# ----------------------------------------------------------------- records --


@dataclasses.dataclass(frozen=True)
class Usage:
    """One turn's tokens. `cache_read`/`cache_write` are 0 where the backend
    does not report them, which is why CACHE_USAGE is a declared capability --
    zero and "not measured" are not the same fact and only one of them is
    worth acting on."""

    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0


@dataclasses.dataclass(frozen=True)
class ToolUse:
    id: str
    name: str
    arguments: dict


@dataclasses.dataclass(frozen=True)
class ToolResult:
    id: str
    name: str
    content: str
    #: A refusal is a RESULT, not a transport failure: the model has to read it
    #: and change course. Protocols that carry no error flag on a tool result
    #: must say so in the content -- see the OpenAI backend.
    is_error: bool = False


# Stop reasons, normalised. A backend maps its provider's vocabulary onto
# these; anything it cannot map becomes `answer`, because a turn with no tool
# calls is an answer whatever the provider called it.
ANSWER = "answer"
TOOL_USE_STOP = "tool_use"
REFUSED = "refusal"
PAUSED = "paused"


@dataclasses.dataclass(frozen=True)
class Turn:
    text: str
    tool_uses: tuple[ToolUse, ...]
    stop_reason: str
    usage: Usage
    #: What the provider actually called it, kept for the audit record: the
    #: normalised reason is what the loop branches on, and the raw one is what
    #: somebody reading a log will want to search for.
    raw_stop_reason: str = ""


# ---------------------------------------------------------------- the seam --


@runtime_checkable
class Conversation(Protocol):
    """One exchange with a model, holding the transcript in its own shape."""

    def ask(self, text: str) -> Turn:
        """Add the caller's question and take the next turn."""

    def give(self, results: list[ToolResult]) -> Turn:
        """Hand back what the tools returned and take the next turn.

        An EMPTY list continues without adding anything, which is how a
        provider that paused mid-turn is resumed.
        """


@runtime_checkable
class ModelBackend(Protocol):
    capabilities: frozenset[str]

    def start(
        self,
        *,
        system: list[dict],
        tools: list[dict],
        model: str,
        effort: str,
        user: str,
        history: list[Any] | None = None,
    ) -> Conversation:
        """Begin a conversation.

        `tools` are MCP tool definitions -- `{name, description, inputSchema}`
        -- and each backend renders them into its own shape. MCP is the source
        of truth because it is the one shape that is nobody's provider.

        `user` is the caller's label (agent/caller.py): a keyed pseudonym, or
        "" when there is no key to make one.
        """
