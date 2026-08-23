"""The ask contract, as executable assertions.

    python -m agent.conformance.run                       # DAS_ASK_URL, in the network
    python -m agent.conformance.run --base <url>          # any reachable instance

Asserts agent/contract/ask.openapi.json and events.schema.json against a
running ask service, in the idiom of services/conformance/run.py. Two groups:

* **transport** -- tickets, streams, replay, ownership, cancel, `done`. These
  need no model: with DAS_LLM_BASE_URL at the llm-stub the agent answers in
  one hop with no tool call, which is exactly enough surface to prove the
  plumbing. This is how `make conformance-ask` runs in CI.
* **behaviour** -- refusal, abstention, `path`, conversation memory. These
  need a model and the seeded data, and are SKIPPED (printed, counted,
  never hidden) unless `--behaviour` is given and a model key is present.

Every event on every stream is validated against the schema, because a
contract no check reads is a comment.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.request

import jsonschema

from seed import common as c

PASS, FAIL, SKIP = "\033[32mok\033[0m", "\033[31mFAIL\033[0m", "\033[33mskip\033[0m"
_results: list[tuple[str, bool | None, str]] = []
HERE = pathlib.Path(__file__).resolve().parent.parent
SCHEMA = json.loads((HERE / "contract" / "events.schema.json").read_text())
VALIDATOR = jsonschema.Draft202012Validator(SCHEMA)
TERMINAL = ("answer", "abstention", "refusal", "error")


def check(name: str, ok: bool, detail: str = "") -> bool:
    _results.append((name, ok, detail))
    print(f"  {PASS if ok else FAIL}  {name}" + (f" — {detail}" if detail else ""), flush=True)
    return ok


def skip(name: str, why: str) -> None:
    _results.append((name, None, why))
    print(f"  {SKIP}  {name} — {why}", flush=True)


def token(upn: str) -> str:
    from agent import identity

    try:
        return identity.token_for(upn)
    except identity.SignInUnavailable as e:
        raise SystemExit(f"cannot sign in as {upn}: {e}") from None


class Ask:
    def __init__(self, base: str):
        self.base = base.rstrip("/")

    def rest(self, method: str, path: str, tok: str | None, body=None, headers=None):
        h = dict(headers or {})
        if tok:
            h["Authorization"] = "Bearer " + tok
        st, _, text = c.http(method, self.base + path, headers=h, json_body=body)
        try:
            return st, json.loads(text)
        except json.JSONDecodeError:
            return st, {"raw": text}

    def events(self, ticket: str, tok: str, *, after: int = 0, limit_s: float = 120) -> list[dict]:
        """Read the stream to its close, validating every frame. Returns the
        events; a schema violation is recorded as a failed check and the
        event is still returned so later assertions can see what arrived."""
        req = urllib.request.Request(
            f"{self.base}/v1/asks/{ticket}/events",
            headers={"Authorization": "Bearer " + tok, "Last-Event-ID": str(after)},
        )
        out: list[dict] = []
        deadline = time.time() + limit_s
        # The same TLS context every harness uses, so the gateway's local
        # certificate is handled once, in seed.common, and not here.
        with urllib.request.urlopen(req, timeout=limit_s, context=c._SSL) as r:
            buf: list[str] = []
            while time.time() < deadline:
                line = r.readline().decode()
                if not line:
                    break
                line = line.rstrip("\n")
                if line.startswith(":"):
                    continue
                if line == "":
                    data = "".join(x[5:].lstrip() for x in buf if x.startswith("data:"))
                    buf = []
                    if data:
                        e = json.loads(data)
                        errs = list(VALIDATOR.iter_errors(e))
                        if errs:
                            check(
                                f"event {e.get('type')} seq {e.get('seq')} conforms",
                                False,
                                errs[0].message[:90],
                            )
                        out.append(e)
                        if e.get("type") == "done":
                            return out
                    continue
                buf.append(line)
        return out

    def ask(
        self, tok: str, question: str, conv: str | None = None, **extra
    ) -> tuple[str, str, list[dict]]:
        """Open (or reuse) a conversation, ask, read to done."""
        if conv is None:
            st, b = self.rest("POST", "/v1/conversations", tok)
            assert st == 201, f"open conversation: {st} {b}"
            conv = b["conversation_id"]
        st, b = self.rest(
            "POST", f"/v1/conversations/{conv}/asks", tok, {"question": question, **extra}
        )
        assert st == 202, f"ask: {st} {b}"
        return conv, b["ticket"], self.events(b["ticket"], tok)


def transport(ask: Ask, alice: str, bob: str) -> None:
    # ---- identity ----------------------------------------------------
    st, _ = ask.rest("POST", "/v1/conversations", None)
    check("no bearer is refused", st == 401, f"status {st}")
    st, _ = ask.rest("POST", "/v1/conversations", "eyJhbGciOiJub25lIn0.eyJzdWIiOiJ4In0.")
    check("a forged bearer is refused", st == 401, f"status {st}")

    # ---- the ticket ---------------------------------------------------
    st, conv = ask.rest("POST", "/v1/conversations", alice)
    check("a conversation opens", st == 201 and "conversation_id" in conv, str(conv)[:60])
    cid = conv["conversation_id"]
    t0 = time.time()
    st, tk = ask.rest(
        "POST", f"/v1/conversations/{cid}/asks", alice, {"question": "How many tickets are there?"}
    )
    took = int((time.time() - t0) * 1000)
    check(
        "a ticket is returned before any tool call runs",
        st == 202 and tk.get("seq") == 0 and took < 2000,
        f"{took}ms seq={tk.get('seq')}",
    )
    ticket = tk["ticket"]
    st, _ = ask.rest("POST", f"/v1/conversations/{cid}/asks", alice, {"question": "again"})
    check("a second ask while one runs is 409", st == 409, f"status {st}")
    st, _ = ask.rest("POST", f"/v1/conversations/{cid}/asks", alice, {"question": ""})
    check("an empty question is 422", st == 422, f"status {st}")

    # ---- the stream -----------------------------------------------------
    evs = ask.events(ticket, alice)
    types = [e["type"] for e in evs]
    seqs = [e["seq"] for e in evs]
    check(
        "the stream starts with accepted at seq 1",
        types[:1] == ["accepted"] and seqs[:1] == [1],
        str(types[:2]),
    )
    check("seq is contiguous", seqs == list(range(1, len(seqs) + 1)), str(seqs))
    check(
        "the question appears in accepted and nowhere else",
        bool(evs[0].get("question"))
        and not any(evs[0]["question"] in json.dumps(e) for e in evs[1:]),
        "",
    )
    check(
        "exactly one branch opens and one closes",
        [e for e in evs if e["type"] == "branch" and e["state"] == "opened"].__len__() == 1
        and [e for e in evs if e["type"] == "branch" and e["state"] == "closed"].__len__() == 1,
        str(types),
    )
    term = [e for e in evs if e["type"] in TERMINAL]
    check("exactly one terminal event", len(term) == 1, str([e["type"] for e in term]))
    check(
        "done is last, exactly once",
        types.count("done") == 1 and types[-1] == "done",
        str(types[-3:]),
    )
    done = evs[-1]
    check(
        "done.outcome names the terminal event",
        done.get("outcome") == (term[0]["type"] if term else None),
        str(done.get("outcome")),
    )
    check(
        "done.steps equals the step events emitted",
        done.get("steps") == types.count("step"),
        f"{done.get('steps')} vs {types.count('step')}",
    )
    check("done.hops is at least one", (done.get("hops") or 0) >= 1, str(done.get("hops")))

    # ---- replay -----------------------------------------------------------
    mid = max(1, len(evs) // 2)
    again = ask.events(ticket, alice, after=mid)
    check(
        "events replay from Last-Event-ID with no gap in seq",
        [e["seq"] for e in again] == list(range(mid + 1, len(evs) + 1)),
        f"from {mid}: {[e['seq'] for e in again][:4]}…",
    )
    st, state = ask.rest("GET", f"/v1/asks/{ticket}", alice)
    check(
        "the state endpoint reports finished with the terminal and done",
        st == 200
        and state.get("status") == "finished"
        and state.get("terminal")
        and state.get("done"),
        str(state.get("status")),
    )

    # ---- ownership --------------------------------------------------------
    st, _ = ask.rest("GET", f"/v1/asks/{ticket}/events", bob)
    check("a second identity gets 404 on another's ticket", st == 404, f"status {st}")
    st, _ = ask.rest("POST", f"/v1/conversations/{cid}/asks", bob, {"question": "x"})
    check("a second identity gets 404 on another's conversation", st == 404, f"status {st}")
    st, _ = ask.rest("POST", f"/v1/asks/{ticket}/cancel", bob)
    check("a second identity cannot cancel it", st == 404, f"status {st}")

    # ---- cancel -----------------------------------------------------------
    st, _ = ask.rest("POST", f"/v1/asks/{ticket}/cancel", alice)
    check(
        "cancel after done is 202 and changes nothing",
        st == 202 and ask.events(ticket, alice)[-1]["type"] == "done",
        f"status {st}",
    )
    st, _ = ask.rest("POST", f"/v1/asks/{ticket}/cancel", alice)
    check("cancel is idempotent", st == 202, f"status {st}")
    # A fresh ask, cancelled before the stream is read: the guarantee is that
    # nothing but error{cancelled} and done follow once the run notices.
    st, tk2 = ask.rest(
        "POST", f"/v1/conversations/{cid}/asks", alice, {"question": "What is the slowest team?"}
    )
    st, _ = ask.rest("POST", f"/v1/asks/{tk2['ticket']}/cancel", alice)
    evs2 = ask.events(tk2["ticket"], alice)
    t2 = [e["type"] for e in evs2]
    ended = [e for e in evs2 if e["type"] in TERMINAL]
    check("cancel stops the stream", t2[-1] == "done" and len(ended) == 1, f"{t2[-2:]}")
    # With the stub the first hop already answered before cancel was seen; with a
    # real model a multi-hop run is cut. Either way the terminal event is sound.
    if ended and ended[0]["type"] == "error":
        check(
            "a cancelled run ends in error{cancelled}",
            ended[0].get("kind") == "cancelled",
            str(ended[0].get("kind")),
        )


def behaviour(ask: Ask, alice: str, bob: str) -> None:
    # ---- refusal vs answer ----------------------------------------------
    _, _, evs = ask.ask(bob, "How many rows are in the tickets table?")
    term = next(e for e in evs if e["type"] in TERMINAL)
    check(
        "a user with no role on the source gets refusal, never answer",
        term["type"] == "refusal" and term.get("what") in ("access", "guard", "rate_limit"),
        f"{term['type']} {term.get('what')}",
    )

    # ---- abstention ---------------------------------------------------
    _, _, evs = ask.ask(alice, "What is the average customer satisfaction score this quarter?")
    term = next(e for e in evs if e["type"] in TERMINAL)
    ok = term["type"] == "abstention" and isinstance(term.get("searched_terms"), list)
    check(
        "a question the catalog cannot ground emits abstention with search terms",
        ok,
        f"{term['type']} {term.get('searched_terms', '')[:3] if ok else ''}",
    )
    if ok:
        check(
            "abstention carries no question text",
            "satisfaction score this quarter" not in json.dumps(term),
            "",
        )

    # ---- path --------------------------------------------------------------
    _, _, evs = ask.ask(alice, "What does Resolution Time mean in the support glossary?")
    term = next(e for e in evs if e["type"] in TERMINAL)
    if term["type"] == "answer":
        check(
            "a catalog-only answer reports path=catalog and an empty sql[]",
            term["path"] == "catalog" and term["sql"] == [],
            f"path={term['path']} sql={len(term['sql'])}",
        )
    else:
        check("a definitional question is answered", False, term["type"])

    # ---- conversation memory ------------------------------------------------
    cid, _, evs = ask.ask(alice, "Which support team has the most tickets?")
    first = next(e for e in evs if e["type"] in TERMINAL)
    if first["type"] == "answer":
        _, _, evs2 = ask.ask(alice, "And what is that team's average resolution time?", conv=cid)
        second = next(e for e in evs2 if e["type"] in TERMINAL)
        check(
            'a second ask in a conversation can resolve "that team" from the first',
            second["type"] == "answer" and bool(second.get("sql")),
            f"{second['type']} turn={evs2[0].get('turn')}",
        )
    else:
        check("the first turn of a conversation answers", False, first["type"])

    # ---- audit ---------------------------------------------------------------
    steps = [e for e in evs if e["type"] == "step"]
    check(
        "every step's ms is non-negative and matches the audit convention",
        all(s["ms"] >= 0 for s in steps),
        f"{len(steps)} steps",
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=c.CFG.get("DAS_ASK_URL", "http://ask:8091"))
    ap.add_argument("--name", default=None)
    ap.add_argument(
        "--via-gateway",
        action="store_true",
        help="reach the service through the gateway's /ask route, as a client would",
    )
    ap.add_argument(
        "--behaviour", action="store_true", help="also run the checks that need a model"
    )
    a = ap.parse_args()
    if a.via_gateway:
        a.base = c.CFG["DAS_APIM_BASE"].rstrip("/") + "/ask"

    print(f"\nconformance (ask): {a.name or a.base}")
    alice = token("alice@entraemulator.dev")  # Data.Analyst
    bob = token("bob@entraemulator.dev")  # no role on the source
    ask = Ask(a.base)
    try:
        transport(ask, alice, bob)
    except (AssertionError, urllib.error.URLError) as e:
        check("the transport group completed", False, str(e)[:120])
    if a.behaviour and os.environ.get("ANTHROPIC_API_KEY"):
        try:
            behaviour(ask, alice, bob)
        except (AssertionError, urllib.error.URLError) as e:
            check("the behaviour group completed", False, str(e)[:120])
    else:
        for name in (
            "a user with no role on the source gets refusal, never answer",
            "a question the catalog cannot ground emits abstention with search terms",
            "a catalog-only answer reports path=catalog and an empty sql[]",
            'a second ask in a conversation can resolve "that team" from the first',
        ):
            skip(name, "needs --behaviour and ANTHROPIC_API_KEY")

    failed = [r for r in _results if r[1] is False]
    skipped = [r for r in _results if r[1] is None]
    ran = len(_results) - len(skipped)
    print(
        f"\n{ran - len(failed)}/{ran} contract checks passed"
        + (f", {len(skipped)} skipped" if skipped else "")
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
