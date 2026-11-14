"""Shared test fakes: a scripted stand-in for the Anthropic client, and guard-call helpers."""

import copy
from datetime import datetime, timezone
from types import SimpleNamespace

from agent.guards import Execute, GuardContext, Outcome, ToolCall


def blk(**kw):
    return SimpleNamespace(**kw, to_dict=lambda: dict(kw))


def text(t):
    return blk(type="text", text=t)


def tool(name, tid, **inp):
    return blk(type="tool_use", id=tid, name=name, input=inp)


def resp(*content):
    stop = "tool_use" if any(c.type == "tool_use" for c in content) else "end_turn"
    usage = {"input_tokens": 10, "output_tokens": 2}
    return SimpleNamespace(model="fake", stop_reason=stop, content=list(content),
                           usage=SimpleNamespace(**usage, to_dict=lambda: dict(usage)))


class FakeLLM:
    """Returns scripted responses in order; records the messages it was sent."""

    def __init__(self, *responses):
        self.script = list(responses)
        self.seen: list[list] = []
        self.messages = self

    async def create(self, **kw):
        self.seen.append(copy.deepcopy(kw["messages"]))
        return self.script.pop(0) if self.script else resp(text("Done."))


def make_ctx(run="r1", tenant="tenant_a", lookup=None, excluded=(), read_only=("list_charges",)):
    return GuardContext(run_id=run, tenant_id=tenant, messages=[], read_only_tools=frozenset(read_only),
                        excluded_tools=frozenset(excluded), _lookup=lookup)


_n = [0]


async def step(guard, ctx, name, args, outcome=None, tid=None):
    """Run one call through a guard the way dispatch_tool_call does; return the decision.

    `outcome` = ("succeeded" | "error" | "timeout", text) is what the tool returns if executed.
    """
    _n[0] += 1
    call = ToolCall(tool_use_id=tid or f"toolu_{_n[0]}", name=name, input=dict(args),
                    ts=datetime.now(timezone.utc))
    decision = await guard.before_tool_call(ctx, call)
    call.decision = decision
    if isinstance(decision, Execute):
        status, out = outcome or ("succeeded", f'{{"ok": "{call.tool_use_id}"}}')
        call.outcome = Outcome(status, out)
        await guard.after_tool_call(ctx, call, call.outcome)
    ctx.calls.append(call)
    return decision
