"""Ask the agent a question.

    make ask Q="What was net revenue by fiscal year?"
    make ask Q="..." ARGS="--user carol@entraemulator.dev --trace"
"""
from __future__ import annotations

import argparse
import sys

from agent import agent as agent_mod
from agent import identity


def main() -> int:
    ap = argparse.ArgumentParser(description="Ask a governed data question.")
    ap.add_argument("question", nargs="*", help="the question")
    ap.add_argument("--user", default=None, help="sign in as this user (else DAS_USER)")
    ap.add_argument("--trace", action="store_true", help="show each tool call")
    ap.add_argument("--no-context", action="store_true",
                    help="ablation: answer without the business catalog")
    ap.add_argument("--model", default=agent_mod.DEFAULT_MODEL)
    ap.add_argument("--effort", default=agent_mod.DEFAULT_EFFORT)
    a = ap.parse_args()

    question = " ".join(a.question).strip()
    if not question:
        ap.error("ask a question: make ask Q=\"...\"")

    token = identity.token_for(a.user)
    answer = agent_mod.ask(question, token, om=not a.no_context, model=a.model, effort=a.effort,
                           on_step=(lambda line: print(line, file=sys.stderr)) if a.trace else None)
    print(answer.text)
    if a.trace:
        print(f"\n  {len(answer.tool_calls)} tool calls · {answer.input_tokens} in / "
              f"{answer.output_tokens} out tokens · {answer.ms}ms", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
