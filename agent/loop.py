"""DoubleTake agent: a Claude billing assistant that talks to the fake payments
MCP server over stdio.

    python -m agent.loop                            # interactive REPL
    python -m agent.loop "charge customer 881 $50"  # one-shot

Every model turn, tool call and tool result is logged to logs/<run_id>.jsonl.

Programmatic use (scripts, benchmarks):

    summary = await run_conversation(["charge customer 881 $50"], db_path, faults_path, model)

Every tool call goes through a guard (agent/guards/) inside dispatch_tool_call;
the default guard "none" always executes. The benchmark lives in bench/.

Environment:
    DOUBLETAKE_DB            SQLite path passed to the server (default data/payments.db)
    DOUBLETAKE_FAULTS        fault config passed to the server (default kitchen/faults.json)
    DOUBLETAKE_TOOL_TIMEOUT  seconds to wait for one tool call (default 10)
"""

import asyncio
import json
import os
import sys
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import anthropic
from dotenv import load_dotenv
from mcp import Client, StdioServerParameters
from mcp.shared.exceptions import MCPError
from mcp_types import REQUEST_TIMEOUT

from agent.guards import (
    NEEDS_CONFIRMATION,
    AskUser,
    Block,
    Execute,
    Guard,
    GuardContext,
    NoneGuard,
    Outcome,
    ReturnResult,
    ToolCall,
)
from agent.guards.base import decision_detail

ROOT = Path(__file__).resolve().parent.parent
SERVER_SCRIPT = ROOT / "kitchen" / "server.py"
LOG_DIR = ROOT / "logs"

SYSTEM_PROMPT = "You are a billing assistant. Complete the user's request using the tools."
MAX_TOKENS = 16000
MAX_MODEL_TURNS = 25  # safety cap per user message
DEFAULT_MODEL = "claude-opus-5-5"
DEFAULT_TOOL_TIMEOUT = 10.0
TIMEOUT_TEXT = "timeout: no response from the service"  # tool-neutral (was "...from payments service" before 2026-11-13)
DEFAULT_TENANT = "tenant_a"


def default_tool_timeout() -> float:
    return float(os.environ.get("DOUBLETAKE_TOOL_TIMEOUT", DEFAULT_TOOL_TIMEOUT))


class TraceLogger:
    """Append-only JSONL trace: one JSON object per line."""

    def __init__(self, run_id: str):
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id
        self.path = LOG_DIR / f"{run_id}.jsonl"
        self._f = self.path.open("a", encoding="utf-8")
        self._seq = 0

    def log(self, event: str, **fields) -> None:
        self._seq += 1
        record = {
            "run_id": self.run_id,
            "seq": self._seq,
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": event,
            **fields,
        }
        self._f.write(json.dumps(record, default=str) + "\n")
        self._f.flush()

    def close(self) -> None:
        self._f.close()


def new_run_id() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]


def mcp_tools_to_anthropic(mcp_tools) -> list[dict]:
    return [
        {
            "name": t.name,
            "description": t.description or "",
            "input_schema": t.input_schema,
        }
        for t in mcp_tools
    ]


def server_params(db_path=None, faults_path=None) -> StdioServerParameters:
    """stdio params for kitchen/server.py. The stdio client only forwards a small
    whitelist of env vars, so DOUBLETAKE_DB / DOUBLETAKE_FAULTS are passed explicitly."""
    env = {}
    if db_path:
        env["DOUBLETAKE_DB"] = str(db_path)
    if faults_path:
        env["DOUBLETAKE_FAULTS"] = str(faults_path)
    return StdioServerParameters(command=sys.executable, args=[str(SERVER_SCRIPT)], cwd=str(ROOT), env=env or None)


async def dispatch_tool_call(
    mcp: Client,
    block,
    trace: TraceLogger,
    timeout: float | None = None,
    *,
    guard: Guard | None = None,
    ctx: GuardContext | None = None,
    forced: bool = False,
) -> dict:
    """Run ONE tool_use block and return the matching tool_result block.

    This is the single choke point for side effects. The guard is consulted
    first, for every call (model-issued or forced); the tool reaches the MCP
    server only if the guard says Execute. The processed call (with decision
    and outcome) is appended to ctx.calls, so the caller can read ctx.calls[-1].

    If the server does not answer within `timeout` seconds, the model gets an
    is_error tool_result saying so. The request may still have taken effect on
    the server; no retry or check happens here (that is a guard's job).
    """
    timeout = default_tool_timeout() if timeout is None else timeout
    guard = guard or NoneGuard()
    if ctx is None:
        ctx = GuardContext(run_id=trace.run_id, tenant_id="", messages=[])
    call = ToolCall(
        tool_use_id=block.id, name=block.name, input=dict(block.input),
        ts=datetime.now(timezone.utc), forced=forced,
    )
    trace.log("tool_call", tool_use_id=block.id, name=block.name, input=block.input, forced=forced)

    decision = await guard.before_tool_call(ctx, call)
    call.decision = decision
    trace.log(
        "guard_decision", tool_use_id=block.id, name=block.name, guard=guard.name,
        decision=decision.kind, detail=decision_detail(decision), forced=forced,
    )

    start = time.perf_counter()
    if isinstance(decision, Execute):
        timed_out = False
        try:
            result = await mcp.call_tool(block.name, block.input, read_timeout_seconds=timeout)
            text = "\n".join(c.text for c in result.content if getattr(c, "type", None) == "text")
            is_error = bool(result.is_error)
        except (MCPError, TimeoutError) as e:
            if isinstance(e, TimeoutError) or e.code == REQUEST_TIMEOUT:
                timed_out = True
                text = TIMEOUT_TEXT
            else:
                text = f"{type(e).__name__}: {e}"
            is_error = True
        except Exception as e:  # transport/protocol failure: report to the model, don't crash
            text = f"{type(e).__name__}: {e}"
            is_error = True
        duration_ms = round((time.perf_counter() - start) * 1000, 2)
        if timed_out:
            trace.log(
                "tool_timeout", tool_use_id=block.id, name=block.name, timeout_s=timeout, duration_ms=duration_ms
            )
        call.outcome = Outcome("timeout" if timed_out else ("error" if is_error else "succeeded"), text)
        await guard.after_tool_call(ctx, call, call.outcome)
    elif isinstance(decision, ReturnResult):
        text, is_error = decision.text, False
    elif isinstance(decision, Block):
        text, is_error = decision.message, True
    elif isinstance(decision, AskUser):
        text, is_error = NEEDS_CONFIRMATION + decision.question, True
    else:
        raise TypeError(f"guard {guard.name} returned {decision!r}")
    duration_ms = round((time.perf_counter() - start) * 1000, 2)

    trace.log(
        "tool_result",
        tool_use_id=block.id,
        name=block.name,
        is_error=is_error,
        output=text,
        duration_ms=duration_ms,
    )
    ctx.calls.append(call)
    return {"type": "tool_result", "tool_use_id": block.id, "content": text, "is_error": is_error}


@dataclass
class ToolCallRecord:
    user_turn: int  # 1-based index into RunSummary.turns
    model_turn: int
    name: str
    input: dict
    is_error: bool
    timed_out: bool
    output: str
    forced: bool = False
    decision: str = "execute"  # guard decision kind; the tool ran only for "execute"


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0

    def add(self, u) -> None:
        for f in ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
            setattr(self, f, getattr(self, f) + (getattr(u, f, None) or 0))

    @property
    def total(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_creation_input_tokens
            + self.cache_read_input_tokens
        )


@dataclass
class TurnRecord:
    user_text: str
    reply: str
    api_error: str | None = None
    source: str = "script"  # "script" | "on_ask" | "forced" (a forced tool call, not a user message)


@dataclass
class RunSummary:
    run_id: str
    trace_path: Path
    model: str
    turns: list[TurnRecord] = field(default_factory=list)
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["trace_path"] = str(self.trace_path)
        d["usage"]["total"] = self.usage.total
        return d


class AgentSession:
    """One agent conversation: an MCP server process, a trace, message history and a guard.

    async with AgentSession(model) as s:
        reply = await s.send("charge customer 881 $50")
    """

    def __init__(
        self,
        model: str,
        db_path: str | Path | None = None,
        faults_path: str | Path | None = None,
        tool_timeout: float | None = None,
        run_id: str | None = None,
        exclude_tools: Sequence[str] = (),
        guard: Guard | None = None,
        tenant_id: str = DEFAULT_TENANT,
        llm=None,
    ):
        self.model = model
        self.db_path = db_path if db_path is not None else os.environ.get("DOUBLETAKE_DB")
        self.faults_path = faults_path if faults_path is not None else os.environ.get("DOUBLETAKE_FAULTS")
        self.tool_timeout = default_tool_timeout() if tool_timeout is None else tool_timeout
        self.run_id = run_id or new_run_id()
        self.exclude_tools = set(exclude_tools)  # hidden from the model and refused if called
        self.guard = guard or NoneGuard()
        self.tenant_id = tenant_id
        self.trace = TraceLogger(self.run_id)
        self.summary = RunSummary(run_id=self.run_id, trace_path=self.trace.path, model=model)
        self.messages: list = []
        self.guard_ctx = GuardContext(run_id=self.run_id, tenant_id=tenant_id, messages=self.messages)
        # Anything with an async .messages.create(**kwargs) works (tests pass a fake).
        self._llm = llm if llm is not None else anthropic.AsyncAnthropic()
        self._client: Client | None = None
        self.mcp: Client | None = None
        self.tools: list[dict] = []

    async def __aenter__(self) -> "AgentSession":
        try:
            self._client = Client(server_params(self.db_path, self.faults_path))
            self.mcp = await self._client.__aenter__()
            mcp_tools = (await self.mcp.list_tools()).tools
        except BaseException:
            self.trace.close()
            raise
        mcp_tools = [t for t in mcp_tools if t.name not in self.exclude_tools]
        self.tools = mcp_tools_to_anthropic(mcp_tools)
        self.trace.log(
            "run_start",
            model=self.model,
            system=SYSTEM_PROMPT,
            tool_timeout_s=self.tool_timeout,
            db_path=str(self.db_path) if self.db_path else None,
            faults_path=str(self.faults_path) if self.faults_path else None,
            faults=_read_json(self.faults_path),
            excluded_tools=sorted(self.exclude_tools),
            guard=self.guard.name,
            tenant_id=self.tenant_id,
            tools=[
                {**t, "annotations": m.annotations.model_dump() if m.annotations else None}
                for t, m in zip(self.tools, mcp_tools)
            ],
        )
        return self

    async def __aexit__(self, *exc) -> None:
        try:
            if self._client is not None:
                await self._client.__aexit__(*exc)
        finally:
            self.trace.log("run_end", usage={**asdict(self.summary.usage), "total": self.summary.usage.total})
            self.trace.close()

    async def send(self, user_text: str, source: str = "script") -> str:
        """Send one user message and run tools until the model stops.

        On an API error the message history is restored to exactly what it was
        before this user message, and the error is re-raised.
        """
        record = TurnRecord(user_text=user_text, reply="", source=source)

        def start() -> None:
            self.messages.append({"role": "user", "content": user_text})
            self.trace.log("user_message", text=user_text, source=source)

        return await self._turn(record, start)

    async def force_tool_call(self, name: str, tool_input: dict) -> str:
        """Inject an assistant tool call as if the model had made it after its last
        reply, run it through the guard exactly like a model call, then let the
        model continue. Returns the model's continuation text.
        """
        if not self.messages or self.messages[-1]["role"] != "assistant":
            raise RuntimeError("force_tool_call needs the conversation to end on an assistant message")
        tool_use_id = "toolu_forced_" + uuid.uuid4().hex[:16]
        record = TurnRecord(user_text=f"[forced {name}({json.dumps(tool_input)})]", reply="", source="forced")
        user_turn = len(self.summary.turns) + 1

        async def start() -> None:
            last = self.messages[-1]
            content = last["content"]
            content = [content] if isinstance(content, str) else list(content)
            content = [{"type": "text", "text": c} if isinstance(c, str) else c for c in content]
            content = [c.to_dict() if hasattr(c, "to_dict") else c for c in content]
            block = {"type": "tool_use", "id": tool_use_id, "name": name, "input": tool_input}
            self.messages[-1] = {"role": "assistant", "content": content + [block]}
            self.trace.log("forced_tool_call", tool_use_id=tool_use_id, name=name, input=tool_input)
            result = await self._run_tool(SimpleNamespace(**block), user_turn, model_turn=0, forced=True)
            self.messages.append({"role": "user", "content": [result]})

        return await self._turn(record, start)

    async def _turn(self, record: TurnRecord, start) -> str:
        checkpoint = len(self.messages)
        saved_last = self.messages[-1] if self.messages else None
        self.summary.turns.append(record)
        user_turn = len(self.summary.turns)
        try:
            res = start()
            if asyncio.iscoroutine(res):
                await res
            record.reply = await self._model_loop(user_turn)
        except anthropic.APIError as e:
            del self.messages[checkpoint:]
            if saved_last is not None and self.messages:
                self.messages[-1] = saved_last  # undo a forced call appended to the last assistant message
            record.api_error = f"{type(e).__name__}: {e}"
            self.trace.log("api_error", error=record.api_error, rolled_back_to=checkpoint)
            raise
        return record.reply

    async def _run_tool(self, block, user_turn: int, model_turn: int, forced: bool = False) -> dict:
        if block.name in self.exclude_tools:  # not offered to the model; never run it
            self.trace.log("tool_refused", tool_use_id=block.id, name=block.name)
            res = {"type": "tool_result", "tool_use_id": block.id,
                   "content": f"unknown tool: {block.name}", "is_error": True}
            decision, timed_out = "refused", False
        else:
            res = await dispatch_tool_call(
                self.mcp, block, self.trace, self.tool_timeout,
                guard=self.guard, ctx=self.guard_ctx, forced=forced,
            )
            call = self.guard_ctx.calls[-1]
            decision = call.decision.kind
            timed_out = call.outcome is not None and call.outcome.status == "timeout"
        self.summary.tool_calls.append(
            ToolCallRecord(
                user_turn=user_turn, model_turn=model_turn, name=block.name, input=dict(block.input),
                is_error=res["is_error"], timed_out=timed_out, output=res["content"],
                forced=forced, decision=decision,
            )
        )
        return res

    async def _model_loop(self, user_turn: int) -> str:
        messages = self.messages
        final_text = ""
        for turn in range(1, MAX_MODEL_TURNS + 1):
            start = time.perf_counter()
            response = await self._llm.messages.create(
                model=self.model,
                max_tokens=MAX_TOKENS,
                system=SYSTEM_PROMPT,
                tools=self.tools,
                messages=messages,
            )
            latency_ms = round((time.perf_counter() - start) * 1000, 2)
            self.summary.usage.add(response.usage)
            self.trace.log(
                "model_turn",
                turn=turn,
                model=response.model,
                request_id=getattr(response, "_request_id", None),
                stop_reason=response.stop_reason,
                latency_ms=latency_ms,
                usage=response.usage.to_dict(),
                content=[b.to_dict() for b in response.content],
            )
            # Append the full content (incl. thinking blocks) so history stays valid.
            messages.append({"role": "assistant", "content": response.content})

            final_text = "\n".join(b.text for b in response.content if b.type == "text")

            if response.stop_reason != "tool_use":
                if response.stop_reason not in ("end_turn", "stop_sequence"):
                    final_text += f"\n[stopped: {response.stop_reason}]"
                return final_text

            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    tool_results.append(await self._run_tool(block, user_turn, turn))
            # All results go back in a single user message.
            messages.append({"role": "user", "content": tool_results})

        self.trace.log("turn_cap_hit", max_model_turns=MAX_MODEL_TURNS)
        return final_text + f"\n[stopped: hit {MAX_MODEL_TURNS} model turns]"


def _read_json(path) -> dict | None:
    if not path:
        return None
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def summary_from_trace(path: Path) -> RunSummary:
    """Rebuild a RunSummary from a JSONL trace (the same fields AgentSession fills in)."""
    events = [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]
    start = next(e for e in events if e["event"] == "run_start")
    s = RunSummary(run_id=start["run_id"], trace_path=Path(path), model=start["model"])
    calls = {e["tool_use_id"]: e for e in events if e["event"] == "tool_call"}
    decisions = {e["tool_use_id"]: e["decision"] for e in events if e["event"] == "guard_decision"}
    refused = {e["tool_use_id"] for e in events if e["event"] == "tool_refused"}
    timeouts = {e["tool_use_id"] for e in events if e["event"] == "tool_timeout"}
    model_turn = 0
    for e in events:
        ev = e["event"]
        if ev == "user_message":
            s.turns.append(TurnRecord(user_text=e["text"], reply="", source=e.get("source", "script")))
            model_turn = 0
        elif ev == "forced_tool_call":
            s.turns.append(TurnRecord(
                user_text=f"[forced {e['name']}({json.dumps(e['input'])})]", reply="", source="forced"))
            model_turn = 0
        elif ev == "model_turn":
            model_turn = e["turn"]
            s.usage.add(SimpleNamespace(**(e.get("usage") or {})))
            s.turns[-1].reply = "\n".join(b["text"] for b in e["content"] if b.get("type") == "text")
        elif ev == "tool_result":
            tid = e["tool_use_id"]
            call = calls.get(tid, {})
            s.tool_calls.append(ToolCallRecord(
                user_turn=len(s.turns), model_turn=model_turn, name=e["name"], input=call.get("input", {}),
                is_error=e["is_error"], timed_out=tid in timeouts, output=e["output"],
                forced=bool(call.get("forced", False)),
                decision="refused" if tid in refused else decisions.get(tid, "execute"),
            ))
        elif ev == "api_error" and s.turns:
            s.turns[-1].api_error = e["error"]
    return s


# A user turn is either fixed text, or a function that looks at the run so far
# and returns the next message (or None to skip it).
UserTurn = str | Callable[[RunSummary], str | None]


async def run_conversation(
    user_turns: Sequence[UserTurn],
    db_path: str | Path | None = None,
    faults_path: str | Path | None = None,
    model: str | None = None,
    tool_timeout: float | None = None,
    run_id: str | None = None,
    exclude_tools: Sequence[str] = (),
    guard: Guard | None = None,
    tenant_id: str = DEFAULT_TENANT,
) -> RunSummary:
    """Run a scripted conversation without the REPL and return its summary.

    Each user turn is sent only after the previous one has finished. Callable
    turns receive the RunSummary so far and may return None to skip. An API
    error stops the conversation (it is recorded in the summary and the trace).
    """
    model = model or os.environ.get("ANTHROPIC_MODEL", DEFAULT_MODEL)
    async with AgentSession(
        model, db_path, faults_path, tool_timeout, run_id, exclude_tools, guard=guard, tenant_id=tenant_id
    ) as session:
        for turn in user_turns:
            text = turn(session.summary) if callable(turn) else turn
            if text is None:
                continue
            try:
                await session.send(text)
            except anthropic.APIError:
                break
    return session.summary


async def main(argv: list[str]) -> int:
    load_dotenv(ROOT / ".env")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY is not set. Copy .env.example to .env and fill it in.", file=sys.stderr)
        return 1
    model = os.environ.get("ANTHROPIC_MODEL", DEFAULT_MODEL)

    async with AgentSession(model) as session:
        print(f"run {session.run_id} | model {model} | tools: {', '.join(t['name'] for t in session.tools)}")
        print(f"trace: {session.trace.path.relative_to(ROOT)}")

        prompts = [" ".join(argv)] if argv else None
        while True:
            if prompts is not None:
                if not prompts:
                    break
                user_text = prompts.pop(0)
                print(f"> {user_text}")
            else:
                try:
                    user_text = input("> ").strip()
                except EOFError:
                    break
                if user_text in ("exit", "quit"):
                    break
                if not user_text:
                    continue
            try:
                reply = await session.send(user_text)
            except anthropic.APIError as e:
                print(f"[API error: {type(e).__name__}: {e}]")
                continue
            print(reply)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
