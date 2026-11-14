"""DoubleTake Phase 0 agent: a Claude billing assistant that talks to the fake
payments MCP server over stdio.

    python -m agent.loop                          # interactive REPL
    python -m agent.loop "charge customer 881 $50"  # one-shot

Every model turn, tool call and tool result is logged to logs/<run_id>.jsonl.
"""

import asyncio
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import anthropic
from dotenv import load_dotenv
from mcp import Client, StdioServerParameters

ROOT = Path(__file__).resolve().parent.parent
SERVER_SCRIPT = ROOT / "kitchen" / "server.py"
LOG_DIR = ROOT / "logs"

SYSTEM_PROMPT = "You are a billing assistant. Complete the user's request using the tools."
MAX_TOKENS = 16000
MAX_MODEL_TURNS = 25  # safety cap per user message


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


def mcp_tools_to_anthropic(mcp_tools) -> list[dict]:
    return [
        {
            "name": t.name,
            "description": t.description or "",
            "input_schema": t.input_schema,
        }
        for t in mcp_tools
    ]


async def dispatch_tool_call(mcp: Client, block, trace: TraceLogger) -> dict:
    """Run ONE tool_use block through MCP and return the matching tool_result block.

    This is the single choke point for side effects. Later phases add a guard
    in front of it (e.g. idempotency / duplicate detection).
    """
    trace.log("tool_call", tool_use_id=block.id, name=block.name, input=block.input)
    start = time.perf_counter()
    try:
        result = await mcp.call_tool(block.name, block.input)
        text = "\n".join(c.text for c in result.content if getattr(c, "type", None) == "text")
        is_error = bool(result.is_error)
    except Exception as e:  # transport/protocol failure: report to the model, don't crash
        text = f"{type(e).__name__}: {e}"
        is_error = True
    duration_ms = round((time.perf_counter() - start) * 1000, 2)
    trace.log(
        "tool_result",
        tool_use_id=block.id,
        name=block.name,
        is_error=is_error,
        output=text,
        duration_ms=duration_ms,
    )
    return {"type": "tool_result", "tool_use_id": block.id, "content": text, "is_error": is_error}


async def run_turn(
    llm: anthropic.AsyncAnthropic,
    model: str,
    mcp: Client,
    tools: list[dict],
    messages: list,
    user_text: str,
    trace: TraceLogger,
) -> str:
    """Handle one user message: loop until the model stops calling tools."""
    messages.append({"role": "user", "content": user_text})
    trace.log("user_message", text=user_text)

    final_text = ""
    for turn in range(1, MAX_MODEL_TURNS + 1):
        start = time.perf_counter()
        response = await llm.messages.create(
            model=model,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            tools=tools,
            messages=messages,
        )
        latency_ms = round((time.perf_counter() - start) * 1000, 2)
        trace.log(
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
                tool_results.append(await dispatch_tool_call(mcp, block, trace))
        # All results go back in a single user message.
        messages.append({"role": "user", "content": tool_results})

    trace.log("turn_cap_hit", max_model_turns=MAX_MODEL_TURNS)
    return final_text + f"\n[stopped: hit {MAX_MODEL_TURNS} model turns]"


async def main(argv: list[str]) -> int:
    load_dotenv(ROOT / ".env")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY is not set. Copy .env.example to .env and fill it in.", file=sys.stderr)
        return 1
    model = os.environ.get("ANTHROPIC_MODEL", "claude-opus-5-5")

    run_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    trace = TraceLogger(run_id)
    llm = anthropic.AsyncAnthropic()  # reads ANTHROPIC_API_KEY from the environment

    # The stdio client only forwards a small whitelist of env vars, so pass the DB override through.
    server_env = {"DOUBLETAKE_DB": os.environ["DOUBLETAKE_DB"]} if os.environ.get("DOUBLETAKE_DB") else None
    params = StdioServerParameters(
        command=sys.executable, args=[str(SERVER_SCRIPT)], cwd=str(ROOT), env=server_env
    )
    try:
        async with Client(params) as mcp:
            mcp_tools = (await mcp.list_tools()).tools
            tools = mcp_tools_to_anthropic(mcp_tools)
            trace.log(
                "run_start",
                model=model,
                system=SYSTEM_PROMPT,
                tools=[{**t, "annotations": m.annotations.model_dump() if m.annotations else None}
                       for t, m in zip(tools, mcp_tools)],
            )
            print(f"run {run_id} | model {model} | tools: {', '.join(t['name'] for t in tools)}")
            print(f"trace: {trace.path.relative_to(ROOT)}")

            messages: list = []
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
                    reply = await run_turn(llm, model, mcp, tools, messages, user_text, trace)
                except anthropic.APIError as e:
                    trace.log("api_error", error=f"{type(e).__name__}: {e}")
                    print(f"[API error: {type(e).__name__}: {e}]")
                    messages.pop()  # drop the unanswered user message
                    continue
                print(reply)
    finally:
        trace.log("run_end")
        trace.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
