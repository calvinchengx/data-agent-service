"""A stand-in for a model API, so the gateway's cost controls can be witnessed.

The gateway's job on the model route is governance: a token ceiling per caller,
and a metric for what was spent. Proving that works needs a backend that
reports token usage — it does not need a model, and it must not need a model
credential, or the check could only run where someone is paying.

So this returns a fixed answer with a REAL usage object, in whichever shape is
asked for:

    POST /openai/...   `usage: {prompt_tokens, completion_tokens, total_tokens}`
    POST /anthropic/... `usage: {input_tokens, output_tokens}`

Both shapes exist here for one reason: the two are counted differently by the
gateway, and which provider you put behind the policy decides what governance
you actually get. That is a fact about the deployment worth demonstrating
rather than asserting — see docs/09-llm-governance.md.

It also CALLS A TOOL when asked, once per conversation, in whichever shape
the protocol uses -- because the two model backends have to be held to the
same behaviour and the interesting half of that behaviour is the tool loop.
Whether to call one is derived from the request rather than from server-side
state: a request that offers tools and carries no result yet gets a call, and
one that carries a result gets the answer. Stateless, so concurrent checks
cannot interleave into each other's turn.

And it REMEMBERS what it was sent, at `GET /requests`. That is what lets a
conformance suite assert on what actually reached the far side -- notably
that a refused tool result arrives somewhere the model would read it, which
is a behaviour the two protocols express completely differently.

This is a TEST DOUBLE. It is never in the request path of the service; the
`llm` API points at it only when `DAS_LLM_BACKEND` says so.
"""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PROMPT_TOKENS = int(os.environ.get("STUB_PROMPT_TOKENS", "120"))
COMPLETION_TOKENS = int(os.environ.get("STUB_COMPLETION_TOKENS", "80"))
ANSWER = os.environ.get("STUB_ANSWER", "Net revenue for FY2025 was 1,905,417.10 USD.")
#: The arguments the stub calls a tool with. Fixed, so a conformance check can
#: assert they survived the round trip through a protocol that carries them as
#: a JSON string and one that carries them as an object.
TOOL_ARGUMENTS = {"sql": "SELECT 1"}
TOOL_CALL_ID = "stub-call-1"

#: The last requests received, newest last, for `GET /requests`.
RECEIVED: list[dict] = []
RECEIVED_MAX = 50


def _tool_to_call(request: dict, already_answered: bool) -> str:
    """The tool to call this turn, or "" to answer instead.

    Derived from the request, never from server state: one call per
    conversation, and a conversation that has already handed a result back
    gets the answer. Two checks running at once cannot take each other's turn.
    """
    if already_answered:
        return ""
    tools = request.get("tools") or []
    if not tools:
        return ""
    first = tools[0]
    # The two protocols name a tool in different places.
    return first.get("name") or (first.get("function") or {}).get("name") or ""


def _anthropic_answered(request: dict) -> bool:
    for message in request.get("messages") or []:
        content = message.get("content")
        if isinstance(content, list) and any(
            isinstance(b, dict) and b.get("type") == "tool_result" for b in content
        ):
            return True
    return False


def _openai_answered(request: dict) -> bool:
    return any(m.get("role") == "tool" for m in request.get("messages") or [])


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:
        """Silence the default per-request line; the driver reports what matters."""

    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/health":
            self._send(200, {"status": "ok"})
            return
        if self.path.startswith("/requests"):
            self._send(200, {"requests": RECEIVED})
            return
        self._send(404, {"error": "not found"})

    def do_DELETE(self) -> None:
        if self.path.startswith("/requests"):
            RECEIVED.clear()
            self._send(200, {"requests": []})
            return
        self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            request = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            self._send(400, {"error": "invalid JSON"})
            return

        RECEIVED.append({"path": self.path, "body": request})
        del RECEIVED[:-RECEIVED_MAX]

        if self.path.startswith("/anthropic"):
            tool = _tool_to_call(request, _anthropic_answered(request))
            if tool:
                self._send(
                    200,
                    {
                        "id": "msg_stub",
                        "type": "message",
                        "role": "assistant",
                        "model": request.get("model", "stub"),
                        "content": [
                            {
                                "type": "tool_use",
                                "id": TOOL_CALL_ID,
                                "name": tool,
                                "input": TOOL_ARGUMENTS,
                            }
                        ],
                        "stop_reason": "tool_use",
                        "usage": {
                            "input_tokens": PROMPT_TOKENS,
                            "output_tokens": COMPLETION_TOKENS,
                        },
                    },
                )
                return
            self._send(
                200,
                {
                    "id": "msg_stub",
                    "type": "message",
                    "role": "assistant",
                    "model": request.get("model", "stub"),
                    "content": [{"type": "text", "text": ANSWER}],
                    "stop_reason": "end_turn",
                    # Anthropic's own field names — deliberately NOT the OpenAI ones.
                    "usage": {"input_tokens": PROMPT_TOKENS, "output_tokens": COMPLETION_TOKENS},
                },
            )
            return

        tool = _tool_to_call(request, _openai_answered(request))
        if tool:
            self._send(
                200,
                {
                    "id": "chatcmpl-stub",
                    "object": "chat.completion",
                    "model": request.get("model", "stub"),
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "tool_calls",
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": TOOL_CALL_ID,
                                        "type": "function",
                                        "function": {
                                            "name": tool,
                                            # A JSON STRING here, an object in the
                                            # Anthropic shape. That difference is
                                            # one of the things conformance exists
                                            # to hold both backends to.
                                            "arguments": json.dumps(TOOL_ARGUMENTS),
                                        },
                                    }
                                ],
                            },
                        }
                    ],
                    "usage": {
                        "prompt_tokens": PROMPT_TOKENS,
                        "completion_tokens": COMPLETION_TOKENS,
                        "total_tokens": PROMPT_TOKENS + COMPLETION_TOKENS,
                    },
                },
            )
            return

        self._send(
            200,
            {
                "id": "chatcmpl-stub",
                "object": "chat.completion",
                "model": request.get("model", "stub"),
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": ANSWER},
                    }
                ],
                "usage": {
                    "prompt_tokens": PROMPT_TOKENS,
                    "completion_tokens": COMPLETION_TOKENS,
                    "total_tokens": PROMPT_TOKENS + COMPLETION_TOKENS,
                },
            },
        )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8095"))
    print(
        f"llm-stub on :{port} ({PROMPT_TOKENS}+{COMPLETION_TOKENS} tokens per answer)", flush=True
    )
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
