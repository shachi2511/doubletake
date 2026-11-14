"""Guard "agent_ledger": the agent-ledger library (pinned in requirements-baselines.txt).

Wiring, following the library's README "Fine-Grained Control" (begin/commit) and
the logic of `EffectLedger.run()`:

- One EffectLedger with a MemoryStore per (tenant_id, run_id).
- ToolCall(workflow_id=run_id, tool=name, args=input, call_id=tool_use_id), with
  the library's default idempotency key: sha256(workflow_id | tool | canonical args).
- Fresh key  -> ledger.begin() -> Execute; after the MCP call, commit:
    succeeded        -> CommitSucceeded(result=text)
    timeout / error  -> CommitFailed(EffectError(message=text)). This is what run()
                        does when a handler raises; a timeout never commits a success.
- Known key  -> the library's own run() resolves it (its handler is never called for a
  replayed key, so we pass one that refuses to run):
    SUCCEEDED -> returns the recorded result         -> ReturnResult(result)
    FAILED    -> raises EffectFailedError(message)  -> Block(message)
    PROCESSING / approval wait timing out -> EffectTimeoutError -> Block(message)
    DENIED / CANCELED -> EffectDeniedError           -> Block(message)
- Read-only tools are not wrapped (the README lists read-only agents under
  "Probably not needed"; replaying a recorded read would return stale data).
"""

from agent_ledger import (
    CommitFailed,
    CommitSucceeded,
    EffectDeniedError,
    EffectError,
    EffectFailedError,
    EffectLedger,
    EffectLedgerOptions,
    EffectTimeoutError,
    MemoryStore,
)
from agent_ledger import ToolCall as LedgerToolCall

from agent.guards.base import Block, Decision, Execute, Guard, GuardContext, Outcome, ReturnResult, ToolCall


class _RunState:
    def __init__(self) -> None:
        self.ledger = EffectLedger(EffectLedgerOptions(store=MemoryStore()))
        self.pending: dict[str, str] = {}  # tool_use_id -> effect id awaiting commit


async def _never(_effect):
    raise RuntimeError("agent_ledger guard: handler must not run for a replayed effect")


class AgentLedgerGuard(Guard):
    name = "agent_ledger"

    def _call(self, ctx: GuardContext, call: ToolCall) -> LedgerToolCall:
        return LedgerToolCall(workflow_id=ctx.run_id, tool=call.name, args=call.input, call_id=call.tool_use_id)

    async def before_tool_call(self, ctx: GuardContext, call: ToolCall) -> Decision:
        if call.name in ctx.read_only_tools:
            return Execute()
        st: _RunState = self.state(ctx, _RunState)
        lcall = self._call(ctx, call)
        existing = await st.ledger.find_by_idem_key(st.ledger.idem_key(lcall))
        if existing is None:
            begin = await st.ledger.begin(lcall)
            st.pending[call.tool_use_id] = begin.effect.id
            return Execute()
        try:
            result = await st.ledger.run(lcall, handler=_never)
        except (EffectFailedError, EffectTimeoutError, EffectDeniedError) as e:
            return Block(str(e))
        return ReturnResult(result if isinstance(result, str) else str(result))

    async def after_tool_call(self, ctx: GuardContext, call: ToolCall, outcome: Outcome) -> None:
        st: _RunState = self.state(ctx, _RunState)
        effect_id = st.pending.pop(call.tool_use_id, None)
        if effect_id is None:
            return
        if outcome.status == "succeeded":
            await st.ledger.commit(effect_id, CommitSucceeded(result=outcome.text))
        else:
            await st.ledger.commit(effect_id, CommitFailed(error=EffectError(message=outcome.text, code=outcome.status)))
