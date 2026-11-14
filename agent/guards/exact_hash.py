"""Guard "exact_hash": the simplest dedup (in the style of tool-call-dedup).

key = tool name + arguments normalized (sorted keys, numbers as floats, strings
stripped). Only SUCCESSFUL results are cached; a repeat with the same key gets
the cached result (ReturnResult) and the tool does not run. Calls that timed out
or errored are not cached, so their repeat executes. Read-only tools always run.
"""

import json

from agent.guards.base import Decision, Execute, Guard, GuardContext, Outcome, ReturnResult, ToolCall


def normalize(v):
    if isinstance(v, bool) or v is None:
        return v
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, dict):
        return {str(k): normalize(v[k]) for k in sorted(v, key=str)}
    if isinstance(v, (list, tuple)):
        return [normalize(x) for x in v]
    return v


def call_key(name: str, args: dict) -> str:
    return name + ":" + json.dumps(normalize(args), sort_keys=True, separators=(",", ":"))


class ExactHashGuard(Guard):
    name = "exact_hash"

    async def before_tool_call(self, ctx: GuardContext, call: ToolCall) -> Decision:
        if call.name in ctx.read_only_tools:
            return Execute()
        cache: dict[str, str] = self.state(ctx, dict)
        hit = cache.get(call_key(call.name, call.input))
        return ReturnResult(hit) if hit is not None else Execute()

    async def after_tool_call(self, ctx: GuardContext, call: ToolCall, outcome: Outcome) -> None:
        if call.name in ctx.read_only_tools or outcome.status != "succeeded":
            return
        self.state(ctx, dict)[call_key(call.name, call.input)] = outcome.text
