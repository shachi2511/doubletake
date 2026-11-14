"""Guard interface: every tool call (model-issued or forced) passes through a guard
inside dispatch_tool_call, before anything reaches the MCP server.

    decision = await guard.before_tool_call(ctx, call)   # Execute | ReturnResult | Block | AskUser
    ... the tool runs only for Execute ...
    await guard.after_tool_call(ctx, call, outcome)      # only for executed calls

What the model sees for each decision:
    Execute          the real tool result
    ReturnResult     `text` as a normal (is_error=false) tool result; the tool is not run
    Block            `message` as an is_error tool result; the tool is not run
    AskUser          "NEEDS_CONFIRMATION: <question>" as an is_error tool result; the tool
                     is not run, and the benchmark runner answers as the user
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

# ---------------------------------------------------------------- decisions


@dataclass(frozen=True)
class Execute:
    kind: Literal["execute"] = "execute"


@dataclass(frozen=True)
class ReturnResult:
    text: str
    kind: Literal["return_result"] = "return_result"


@dataclass(frozen=True)
class Block:
    message: str
    kind: Literal["block"] = "block"


@dataclass(frozen=True)
class AskUser:
    question: str
    kind: Literal["ask_user"] = "ask_user"


Decision = Execute | ReturnResult | Block | AskUser

NEEDS_CONFIRMATION = "NEEDS_CONFIRMATION: "


def decision_detail(d: Decision) -> str | None:
    return getattr(d, "text", None) or getattr(d, "message", None) or getattr(d, "question", None)


# ---------------------------------------------------------------- calls and context

OutcomeStatus = Literal["succeeded", "error", "timeout"]


@dataclass(frozen=True)
class Outcome:
    status: OutcomeStatus
    text: str


@dataclass
class ToolCall:
    """One tool call in this run. `decision` and `outcome` are filled in as it is processed;
    `outcome` stays None when the guard did not let the tool run."""

    tool_use_id: str
    name: str
    input: dict
    ts: datetime  # when the call was requested
    forced: bool = False  # injected by the benchmark, not chosen by the model
    decision: Decision | None = None
    outcome: Outcome | None = None


@dataclass
class GuardContext:
    run_id: str
    tenant_id: str  # from session config, never from the model
    messages: list  # the full message history so far (live; do not mutate)
    calls: list[ToolCall] = field(default_factory=list)  # every EARLIER tool call in this run


class Guard:
    """Base guard: subclasses override one or both hooks."""

    name = "base"

    async def before_tool_call(self, ctx: GuardContext, call: ToolCall) -> Decision:
        return Execute()

    async def after_tool_call(self, ctx: GuardContext, call: ToolCall, outcome: Outcome) -> None:
        return None
