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


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # noqa: A003 — quieter than the default
        pass

    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self._send(200, {"status": "ok"})
            return
        self._send(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            request = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            self._send(400, {"error": "invalid JSON"})
            return

        if self.path.startswith("/anthropic"):
            self._send(200, {
                "id": "msg_stub", "type": "message", "role": "assistant",
                "model": request.get("model", "stub"),
                "content": [{"type": "text", "text": ANSWER}],
                "stop_reason": "end_turn",
                # Anthropic's own field names — deliberately NOT the OpenAI ones.
                "usage": {"input_tokens": PROMPT_TOKENS, "output_tokens": COMPLETION_TOKENS},
            })
            return

        self._send(200, {
            "id": "chatcmpl-stub", "object": "chat.completion",
            "model": request.get("model", "stub"),
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": ANSWER}}],
            "usage": {"prompt_tokens": PROMPT_TOKENS,
                      "completion_tokens": COMPLETION_TOKENS,
                      "total_tokens": PROMPT_TOKENS + COMPLETION_TOKENS},
        })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8095"))
    print(f"llm-stub on :{port} "
          f"({PROMPT_TOKENS}+{COMPLETION_TOKENS} tokens per answer)", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
