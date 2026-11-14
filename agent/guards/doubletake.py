"""Guard "doubletake" for the benchmark: a thin adapter over the `doubletake` package.

All decision logic (effect ledger, per-turn allowance, intent judge, honest messages) lives in
doubletake/ (see doubletake/guard.py). This module only connects it to the bench:

- every non-read-only tool the server offers is registered on first sight; charge_card and send_email
  keep their Phase 4 per-tool targets and descriptions, other tools use the generic target rule;
- charge_card gets a read-back through ctx.lookup("list_charges") (the run's MCP client);
- judge calls and decisions are written to the run's trace via ctx._log (judge tokens are tallied there);
- package decisions are mapped to the bench's decision types.

"doubletake_rules" is the ablation: the same guard with RulesJudge (no LLM).
"""

import contextvars
import json
from datetime import datetime

import doubletake as dt
from doubletake import judge as _judge
from doubletake.judge import JUDGE_MODEL, JUDGE_SCHEMA, JUDGE_SYSTEM, OUTCOME_WORDS, evidence_found  # noqa: F401
from doubletake.store import Effect  # noqa: F401  (re-exported for bench.judge_eval)
from doubletake.targets import is_free_text  # noqa: F401

from agent.guards.base import (
    AskUser,
    Block,
    Decision,
    Execute,
    Guard,
    GuardContext,
    LookupRefused,
    Outcome,
    ReturnResult,
    ToolCall,
)

HaikuJudge = dt.HaikuJudge
RulesJudge = dt.RulesJudge

# Per-tool config used in Phases 2-5 (targets and the wording the judge sees).
TARGET_FIELDS: dict[str, tuple[str, ...]] = {
    "charge_card": ("customer", "amount"),
    "send_email": ("to", "subject"),
}


def describe(tool: str, target: tuple) -> str:
    t = dict(target)
    if tool == "charge_card":
        cents = t.get("amount")
        amount = f"${cents / 100:.2f}" if isinstance(cents, int) else str(cents)
        return f"charge customer {t.get('customer')} {amount}"
    if tool == "send_email":
        return f"email {t.get('to')} with subject \"{t.get('subject')}\""
    return dt.targets.generic_description(tool, target)


def target_of(tool: str, args: dict) -> tuple:
    return dt.target_of(args, TARGET_FIELDS.get(tool))


def judge_prompt(users: list[str], earlier: list, tool: str, target: tuple) -> str:
    return _judge.judge_prompt(users, earlier, tool, target, describe)


def user_messages(messages: list) -> list[str]:
    """The user's own messages, in order (tool results are not user-authored)."""
    return [m["content"] for m in messages if m.get("role") == "user" and isinstance(m.get("content"), str)]


# The run context of the call being checked (read-back and trace logging need the run's MCP client / trace).
_CTX: contextvars.ContextVar[GuardContext] = contextvars.ContextVar("doubletake_ctx")


def _find_charge(effect: Effect, out: Outcome):
    t = dict(effect.target)
    try:
        charges = json.loads(out.text).get("charges", [])
    except ValueError:
        return None
    for c in charges:
        try:
            if c.get("amount_cents") == t.get("amount") and datetime.fromisoformat(c["created_at"]) >= effect.ts:
                return c
        except (KeyError, ValueError):
            continue
    return False


async def _charge_read_back(effect: Effect) -> dt.ReadBack:
    ctx = _CTX.get()
    try:
        out = await ctx.lookup("list_charges", {"customer": dict(effect.target)["customer"]})
    except LookupRefused:
        return dt.ReadBack("unavailable")
    found = _find_charge(effect, out) if out.status == "succeeded" else None
    if found:
        return dt.ReadBack("found", found)
    return dt.ReadBack("absent") if found is False else dt.ReadBack("error")


def _log(event: str, **fields) -> None:
    ctx = _CTX.get(None)
    if ctx is not None and ctx._log:
        ctx._log(event, **fields)


_TO_BENCH = {
    dt.Execute: lambda d: Execute(),
    dt.ReturnResult: lambda d: ReturnResult(d.text),
    dt.Block: lambda d: Block(d.message),
    dt.AskUser: lambda d: AskUser(d.question),
}


class DoubleTakeGuard(Guard):
    name = "doubletake"
    use_judge = True

    def __init__(self, judge=None):
        judge = judge or (HaikuJudge() if self.use_judge else RulesJudge())
        self.judge = judge if getattr(judge, "uses_llm", True) else None
        self.core = dt.DoubleTake(judge=judge, on_event=_log)

    def _register(self, tool: str) -> None:
        if tool not in self.core.tools:
            self.core.register_tool(
                tool, target_fields=TARGET_FIELDS.get(tool), describe=describe,
                read_back=_charge_read_back if tool == "charge_card" else None,
                read_back_name="list_charges",
            )

    def effects(self, ctx: GuardContext) -> list[Effect]:
        return self.core.effects(ctx.run_id, tenant_id=ctx.tenant_id)

    async def before_tool_call(self, ctx: GuardContext, call: ToolCall) -> Decision:
        if call.name in ctx.read_only_tools:
            return Execute()
        self._register(call.name)
        token = _CTX.set(ctx)
        try:
            d = await self.core.check(ctx.run_id, user_messages(ctx.messages), call.name, call.input,
                                      tenant_id=ctx.tenant_id, call_id=call.tool_use_id, at=call.ts)
        finally:
            _CTX.reset(token)
        return _TO_BENCH[type(d)](d)

    async def after_tool_call(self, ctx: GuardContext, call: ToolCall, outcome: Outcome) -> None:
        await self.core.record(ctx.run_id, call.name, call.input, dt.Outcome(outcome.status, outcome.text),
                               tenant_id=ctx.tenant_id, call_id=call.tool_use_id)


class DoubleTakeRulesGuard(DoubleTakeGuard):
    """Ablation: RulesJudge (no LLM). Allowance is always 1 and every new user turn counts as new."""

    name = "doubletake_rules"
    use_judge = False
