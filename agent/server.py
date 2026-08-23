"""The ask service: a ticket now, events as they happen, the answer when there is one.

    python -m agent.server            # DAS_ASK_PORT, default 8091

Serves agent/contract/ask.openapi.json. Every event conforms to
agent/contract/events.schema.json, and agent/conformance/run.py is what says
so. docs/20-ask-service.md is the prose.

Stdlib HTTP on purpose, like the MCP client beside it: the service is a loop
and a dictionary, and the dictionary is the whole design -- tickets and
conversations live in this process for DAS_ASK_TTL_S and are never written
anywhere. The question text exists in one event and in that memory, which is
the only privacy claim that survives (promoter/__init__.py).

What this does NOT do: hold a secret (the caller's token is used as received
and forgotten with the ticket), decide anything about the data (every tool
call goes through the gateway as the caller, and the executor refuses what it
refuses), or narrate. It emits structure; clients render.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import os
import secrets
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from agent import agent as agent_mod
from agent import authn

PORT = int(os.environ.get("DAS_ASK_PORT", "8091"))
TTL_S = int(os.environ.get("DAS_ASK_TTL_S", "900"))
MAX_EVENTS = int(os.environ.get("DAS_ASK_MAX_EVENTS", "1000"))
KEEPALIVE_S = 15
CONTRACT_VERSION = "1"
TERMINAL = ("answer", "abstention", "refusal", "error")


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def audit(**fields: Any) -> None:
    """One JSON line per ask on stdout -- the executor's convention, so the
    same collector reads both. Carries who and what happened, never the
    question."""
    print("audit " + json.dumps({"op": "ask", **fields}, separators=(",", ":")), flush=True)


@dataclasses.dataclass
class Conversation:
    id: str
    oid: str
    history: list[dict] = dataclasses.field(default_factory=list)
    turns: int = 0
    running: str | None = None  # ticket id
    touched: float = dataclasses.field(default_factory=time.time)


@dataclasses.dataclass
class Ticket:
    id: str
    conversation: Conversation
    oid: str
    turn: int
    events: list[dict] = dataclasses.field(default_factory=list)
    dropped: int = 0
    seq: int = 0
    finished_at: float | None = None
    cancel: threading.Event = dataclasses.field(default_factory=threading.Event)
    cond: threading.Condition = dataclasses.field(default_factory=threading.Condition)

    @property
    def finished(self) -> bool:
        return self.finished_at is not None

    def emit(self, type_: str, **payload: Any) -> dict:
        with self.cond:
            self.seq += 1
            event = {
                "seq": self.seq,
                "ticket": self.id,
                "conversation_id": self.conversation.id,
                "ts": _now(),
                "type": type_,
                **payload,
            }
            self.events.append(event)
            # Past the ceiling, diagnostics go first and nothing else goes:
            # a client that reconnects late may miss a step, never an answer.
            while len(self.events) > MAX_EVENTS:
                i = next((k for k, e in enumerate(self.events) if e["type"] == "step"), None)
                if i is None:
                    break
                del self.events[i]
                self.dropped += 1
            if type_ == "done":
                self.finished_at = time.time()
            self.cond.notify_all()
            return event


class Store:
    """Tickets and conversations, in memory, reaped after TTL_S idle."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.conversations: dict[str, Conversation] = {}
        self.tickets: dict[str, Ticket] = {}
        threading.Thread(target=self._reap, daemon=True, name="ask-reaper").start()

    def _reap(self) -> None:
        while True:
            time.sleep(30)
            cutoff = time.time() - TTL_S
            with self.lock:
                for tid in [
                    t for t, v in self.tickets.items() if v.finished_at and v.finished_at < cutoff
                ]:
                    del self.tickets[tid]
                for cid in [
                    c
                    for c, v in self.conversations.items()
                    if v.running is None and v.touched < cutoff
                ]:
                    del self.conversations[cid]

    def conversation(self, cid: str, oid: str) -> Conversation | None:
        """Another identity's conversation does not exist, as far as it can tell."""
        with self.lock:
            c = self.conversations.get(cid)
            return c if c and c.oid == oid else None

    def ticket(self, tid: str, oid: str) -> Ticket | None:
        with self.lock:
            t = self.tickets.get(tid)
            return t if t and t.oid == oid else None


STORE = Store()


def _expires(base: float) -> str:
    return (
        dt.datetime.fromtimestamp(base + TTL_S, dt.UTC)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


# ------------------------------------------------------------------ the run --
def run(ticket: Ticket, question: str, token: str, *, om: bool, model: str, effort: str) -> None:
    conv = ticket.conversation
    branch = "b1"
    ticket.emit("accepted", question=question, turn=ticket.turn, contract_version=CONTRACT_VERSION)
    ticket.emit("branch", branch_id=branch, state="opened", role="supervisor", source=None)
    started = time.time()
    answer: agent_mod.Answer | None = None
    outcome = "error"
    try:
        answer = agent_mod.ask(
            question,
            token,
            om=om,
            model=model,
            effort=effort,
            history=list(conv.history),
            on_event=lambda e: _relay(ticket, branch, e),
            cancelled=ticket.cancel.is_set,
        )
        ticket.emit("branch", branch_id=branch, state="closed", role="supervisor", source=None)
        outcome = _terminal(ticket, answer)
        if outcome == "answer":
            conv.history += [
                {"role": "user", "content": question},
                {"role": "assistant", "content": answer.text},
            ]
    except Exception as e:  # noqa: BLE001 — the run's failure is the ticket's terminal event, not the server's
        ticket.emit("branch", branch_id=branch, state="closed", role="supervisor", source=None)
        ticket.emit("error", kind="internal", detail=f"{type(e).__name__}: {str(e)[:300]}")
    finally:
        a = answer or agent_mod.Answer("", [])
        ticket.emit(
            "done",
            outcome=outcome,
            hops=a.hops,
            steps=len(a.tool_calls),
            tokens_in=a.input_tokens,
            tokens_out=a.output_tokens,
            cache_read_tokens=a.cache_read_tokens,
            cache_write_tokens=a.cache_write_tokens,
            ms=int((time.time() - started) * 1000),
            model=model,
            effort=effort,
        )
        audit(
            oid=ticket.oid,
            ticket=ticket.id,
            conversation=conv.id,
            turn=ticket.turn,
            verdict=outcome,
            steps=len(a.tool_calls),
            sources=a.sources,
            tables=sorted(a.tables),
            ms=int((time.time() - started) * 1000),
        )
        with STORE.lock:
            conv.running = None
            conv.touched = time.time()


def _relay(ticket: Ticket, branch: str, e: dict) -> None:
    """A step or milestone from the agent, stamped with the branch it ran in."""
    ticket.emit(e.pop("type"), branch_id=branch, **e)


def _terminal(ticket: Ticket, a: agent_mod.Answer) -> str:
    """The one terminal event the answer mechanically is. Order matters: a
    cancel beats everything, a refusal beats an abstention, and only a run
    that ran something is an answer."""
    if a.stop_reason == "cancelled":
        ticket.emit("error", kind="cancelled", detail="cancelled by the caller")
        return "error"
    if a.stop_reason == "max_steps":
        ticket.emit("error", kind="max_steps", detail=a.text)
        return "error"
    if a.stop_reason == "refusal":
        ticket.emit("error", kind="model", detail=a.text)
        return "error"
    if a.refused:
        bad = next(c for c in a.tool_calls if c.is_error)
        what = (
            "rate_limit"
            if "429" in bad.result
            else "guard"
            if "refused" in bad.result.lower()
            else "access"
        )
        ticket.emit(
            "refusal",
            source=str(bad.arguments.get("source") or "") or None,
            what=what,
            detail=bad.result[:500],
        )
        return "refusal"
    if a.abstained:
        ticket.emit("abstention", text=a.text, searched_terms=a.searched_terms)
        return "abstention"
    ticket.emit(
        "answer",
        text=a.text,
        path=a.path,
        result=_result(a),
        headline=None,
        definitions_applied=a.definitions_applied,
        sources=a.sources,
        tables=sorted(a.tables),
        sql=a.sql,
        caveats=[],
        divergence=None,
    )
    return "answer"


def _result(a: agent_mod.Answer) -> dict | None:
    """The last statement's rows, as the executor returned them."""
    for call in reversed(a.tool_calls):
        if call.name.endswith("run_query") and not call.is_error:
            try:
                p = json.loads(call.result)
                return {
                    "columns": p.get("columns", []),
                    "rows": p.get("rows", []),
                    "truncated": bool(p.get("truncated", False)),
                }
            except (json.JSONDecodeError, AttributeError):
                return None
    return None


# ------------------------------------------------------------------- HTTP --
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ask/1"

    def log_message(self, format: str, *args: object) -> None:
        """The audit line is the record; the access log is noise."""

    # -- plumbing
    def _json(self, status: int, payload: dict | None = None, headers: dict | None = None) -> None:
        body = json.dumps(payload).encode() if payload is not None else b""
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        try:
            parsed = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def _who(self) -> tuple[dict, str] | None:
        try:
            return authn.principal(self.headers.get("Authorization"))
        except authn.Unauthenticated as e:
            self._json(401, {"error": str(e)}, {"WWW-Authenticate": "Bearer"})
        except authn.Forbidden as e:
            self._json(403, {"error": str(e)})
        return None

    # -- routes
    def do_GET(self) -> None:
        if self.path == "/health":
            self._json(200, {"status": "ok", "contract": CONTRACT_VERSION})
            return
        who = self._who()
        if not who:
            return
        claims, _ = who
        oid = claims.get("oid") or claims.get("sub", "")
        parts = self.path.split("?", 1)[0].strip("/").split("/")
        if len(parts) == 4 and parts[:2] == ["v1", "asks"] and parts[3] == "events":
            self._events(parts[2], oid)
        elif len(parts) == 3 and parts[:2] == ["v1", "asks"]:
            self._state(parts[2], oid)
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        who = self._who()
        if not who:
            return
        claims, token = who
        oid = claims.get("oid") or claims.get("sub", "")
        parts = self.path.split("?", 1)[0].strip("/").split("/")
        if parts == ["v1", "conversations"]:
            self._open(oid)
        elif len(parts) == 4 and parts[:2] == ["v1", "conversations"] and parts[3] == "asks":
            self._ask(parts[2], oid, token)
        elif len(parts) == 4 and parts[:2] == ["v1", "asks"] and parts[3] == "cancel":
            self._cancel(parts[2], oid)
        else:
            self._json(404, {"error": "not found"})

    def _open(self, oid: str) -> None:
        conv = Conversation(id=secrets.token_urlsafe(12), oid=oid)
        with STORE.lock:
            STORE.conversations[conv.id] = conv
        self._json(201, {"conversation_id": conv.id, "expires_at": _expires(conv.touched)})

    def _ask(self, cid: str, oid: str, token: str) -> None:
        conv = STORE.conversation(cid, oid)
        if not conv:
            self._json(404, {"error": "not found"})
            return
        body = self._body()
        question = str(body.get("question") or "").strip()
        if not question:
            self._json(422, {"error": "no question"})
            return
        with STORE.lock:
            if conv.running:
                self._json(409, {"error": "an ask is already running in this conversation"})
                return
            conv.turns += 1
            conv.touched = time.time()
            ticket = Ticket(
                id=secrets.token_urlsafe(12), conversation=conv, oid=oid, turn=conv.turns
            )
            STORE.tickets[ticket.id] = ticket
            conv.running = ticket.id
        # The response leaves before the run starts, so a client that opens
        # the stream at once sees seq 1 and misses nothing -- the contract's
        # "ticket before any tool call".
        self._json(
            202,
            {
                "ticket": ticket.id,
                "conversation_id": conv.id,
                "turn": ticket.turn,
                "seq": 0,
                "expires_at": _expires(time.time()),
            },
        )
        threading.Thread(
            target=run,
            args=(ticket, question, token),
            kwargs={
                "om": bool(body.get("context", True)),
                "model": str(body.get("model") or agent_mod.DEFAULT_MODEL),
                "effort": str(body.get("effort") or agent_mod.DEFAULT_EFFORT),
            },
            daemon=True,
            name=f"ask-{ticket.id}",
        ).start()

    def _cancel(self, tid: str, oid: str) -> None:
        t = STORE.ticket(tid, oid)
        if not t:
            self._json(404, {"error": "not found"})
            return
        t.cancel.set()
        self._json(202, {"ticket": tid, "finished": t.finished})

    def _state(self, tid: str, oid: str) -> None:
        t = STORE.ticket(tid, oid)
        if not t:
            self._json(404, {"error": "not found"})
            return
        with t.cond:
            terminal = next((e for e in t.events if e["type"] in TERMINAL), None)
            done = next((e for e in t.events if e["type"] == "done"), None)
            self._json(
                200,
                {
                    "ticket": t.id,
                    "conversation_id": t.conversation.id,
                    "status": "finished" if t.finished else "running",
                    "seq": t.seq,
                    "terminal": terminal,
                    "done": done,
                },
            )

    def _events(self, tid: str, oid: str) -> None:
        t = STORE.ticket(tid, oid)
        if not t:
            self._json(404, {"error": "not found"})
            return
        try:
            after = int(self.headers.get("Last-Event-ID") or 0)
        except ValueError:
            after = 0
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        sent = after
        try:
            while True:
                with t.cond:
                    pending = [e for e in t.events if e["seq"] > sent]
                    if not pending and not t.finished:
                        t.cond.wait(KEEPALIVE_S)
                        pending = [e for e in t.events if e["seq"] > sent]
                if not pending:
                    if t.finished:
                        return
                    self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
                    continue
                for e in pending:
                    frame = f"id: {e['seq']}\nevent: {e['type']}\ndata: {json.dumps(e, separators=(',', ':'))}\n\n"
                    self.wfile.write(frame.encode())
                    sent = e["seq"]
                self.wfile.flush()
                if t.finished and sent >= t.seq:
                    return
        except (BrokenPipeError, ConnectionResetError):
            return  # the client went; the events stay for a reconnect


def main() -> int:
    if not authn.ISSUER or not authn.AUDIENCE:
        print("DAS_ENTRA_ISSUER and DAS_AGENT_AUDIENCE are required", file=sys.stderr)
        return 2
    httpd = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    httpd.daemon_threads = True
    print(f"ask service on :{PORT}  ttl={TTL_S}s  max_events={MAX_EVENTS}", flush=True)
    httpd.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
