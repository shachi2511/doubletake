"""Guard "mycelium": the mycelium-runtime library (pinned in requirements-baselines.txt).

Wiring follows the README's "Manual integration (claim -> execute -> complete)",
which is the documented path when the host owns the tool runner:

- Per (tenant_id, run_id): one ActionLedger (InMemoryLedgerStorage, on_args_drift
  "soft" = the args-drift gate on, a Reconciler for charges) and one LoopGuard
  (InMemoryLoopGuardStorage, default thresholds, missing_run_id_policy "error").
- Bindings: charge_card = keyed_mutate, send_email = non_idempotent_mutate,
  list_charges = read. agent_id "doubletake", policy_version "phase3".
- Every call runs inside execution_scope(TransitionScope(thread_id=run_id,
  run_id=run_id, node="tools")) with kwargs tool_call_id=<our tool_use_id>.
- before_tool_call, in the README's wrapper order (loop_guard before ledger):
    LoopGuard.check(...)                      soft -> ToolBoundaryError, hard -> LedgerHardBlockError
    ledger.derive_request_id(...)
    claim_read_only_async / claim_side_effecting_async   (these poll in-flight peers themselves)
    COMPLETED entry -> ReturnResult(entry.result)       ("return completed")
    side-effecting: record_decision(allowed), and for charge_card attach an
    external_operation_ref (the customer/amount/start time the reconciler needs)
    -> Execute
- after_tool_call: complete(result) on success; fail(failed_after_effect=True) on
  timeout or error ("the provider may have accepted before raising: park ambiguity").
  Reads: fail(failed_after_effect=False).
- Gate mapping:
    ToolBoundaryError (loop soft block, args drift)  -> Block(llm_message)
    LedgerSoftBlockError / LedgerPollTimeoutError    -> Block(message)
    LedgerHardBlockError from the LoopGuard          -> Block(message)  (run frozen)
    LedgerHardBlockError from the ledger             -> AskUser(message) (operator release required)
- Reconciler (charges only): reconcile_async looks for a matching charge created
  at/after the recorded start time via ctx.lookup("list_charges"). Found -> COMPLETED
  with that charge; not found or lookup failed -> UNKNOWN (fail closed: absence in a
  possibly lagging list is not proof of NOT_EXECUTED). There is no email reconciler.
"""

import json
import warnings
from datetime import datetime

from mycelium import (
    ActionLedger,
    InMemoryLedgerStorage,
    InMemoryLoopGuardStorage,
    LedgerHardBlockError,
    LedgerPollTimeoutError,
    LedgerSoftBlockError,
    LoopGuard,
    ReconcileResult,
    TerminalOutcome,
    ToolBoundaryError,
    execution_scope,
)
from mycelium.transition import SideEffectClass, ToolTransitionBinding, TransitionScope

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

CLASSES = {
    "charge_card": SideEffectClass.KEYED_MUTATE,
    "send_email": SideEffectClass.NON_IDEMPOTENT_MUTATE,
    "list_charges": SideEffectClass.READ,
    # Phase 5 kitchen tool, configured as a deployer would (refunds are payment-like); no refund reconciler.
    "issue_refund": SideEffectClass.KEYED_MUTATE,
    "list_refunds": SideEffectClass.READ,
}
BINDINGS = {
    tool: ToolTransitionBinding.for_tool(agent_id="doubletake", policy_version="phase3", side_effect_class=c)
    for tool, c in CLASSES.items()
}


class ChargeReconciler:
    """Read-only: asks list_charges whether the ambiguous charge landed. Never charges."""

    def __init__(self, ctx: GuardContext):
        self.ctx = ctx

    def reconcile(self, entry) -> ReconcileResult:  # sync path is not used (we claim async)
        return ReconcileResult.unknown()

    async def reconcile_async(self, entry) -> ReconcileResult:
        try:
            ref = json.loads(entry.external_operation_ref or "")
            out = await self.ctx.lookup("list_charges", {"customer": ref["customer"]})
        except (ValueError, KeyError, LookupRefused):
            return ReconcileResult.unknown()
        if out.status != "succeeded":
            return ReconcileResult.unknown()
        since = datetime.fromisoformat(ref["started_at"])
        for c in json.loads(out.text).get("charges", []):
            if c["amount_cents"] == ref["amount_cents"] and datetime.fromisoformat(c["created_at"]) >= since:
                return ReconcileResult.completed(json.dumps({**c, "status": "succeeded"}))
        return ReconcileResult.unknown()


class _RunState:
    def __init__(self, ctx: GuardContext):
        self.ledger = ActionLedger(
            storage=InMemoryLedgerStorage(), reconciler=ChargeReconciler(ctx), on_args_drift="soft",
        )
        self.loop = LoopGuard(InMemoryLoopGuardStorage(), missing_run_id_policy="error")
        self.claims: dict[str, tuple[str, int]] = {}  # tool_use_id -> (request_id, fence)


def _scope(ctx: GuardContext):
    return execution_scope(TransitionScope(thread_id=ctx.run_id, run_id=ctx.run_id, node="tools"))


class MyceliumGuard(Guard):
    name = "mycelium"

    def call_kwargs(self, ctx: GuardContext, call: ToolCall) -> dict:
        """Identity kwargs: our tool_use_id as tool_call_id (derived identity, the dev default)."""
        return {**call.input, "tool_call_id": call.tool_use_id}

    async def before_tool_call(self, ctx: GuardContext, call: ToolCall) -> Decision:
        if call.name not in CLASSES:
            return Execute()
        st: _RunState = self.state(ctx, lambda: _RunState(ctx))
        tool, binding = call.name, BINDINGS[call.name]
        kwargs = self.call_kwargs(ctx, call)
        with _scope(ctx), warnings.catch_warnings():
            warnings.simplefilter("ignore")  # in-memory storage warning: expected in a benchmark
            try:
                st.loop.check(tool, (), kwargs, side_effect_class=binding.side_effect_class)
            except ToolBoundaryError as e:
                return Block(e.llm_message or str(e))
            except LedgerHardBlockError as e:
                return Block(str(e))
            try:
                rid = st.ledger.derive_request_id(tool, (), kwargs, transition_binding=binding)
                if binding.side_effect_class == SideEffectClass.READ:
                    entry = await st.ledger.claim_read_only_async(rid, tool, (), kwargs)
                else:
                    entry = await st.ledger.claim_side_effecting_async(rid, tool, (), kwargs, binding)
            except ToolBoundaryError as e:
                return Block(e.llm_message or str(e))
            except (LedgerSoftBlockError, LedgerPollTimeoutError) as e:
                return Block(str(e))
            except LedgerHardBlockError as e:
                return AskUser(str(e))
            if entry.terminal_outcome == TerminalOutcome.COMPLETED.value:
                r = entry.result
                return ReturnResult(r if isinstance(r, str) else json.dumps(r, default=str))
            if binding.side_effect_class != SideEffectClass.READ:
                st.ledger.record_decision(
                    rid, {"allowed": True, "verdicts": [], "denied_reasons": []},
                    expected_owner=entry.owner, expected_fence=entry.fence,
                )
                if tool == "charge_card":
                    ref = {"customer": str(call.input.get("customer", "")),
                           "amount_cents": round(float(call.input.get("amount", 0)) * 100),
                           "started_at": call.ts.isoformat()}
                    entry = st.ledger.attach_external_operation_ref(
                        rid, json.dumps(ref), expected_owner=entry.owner, expected_fence=entry.fence)
            st.claims[call.tool_use_id] = (rid, entry.fence)
        return Execute()

    async def after_tool_call(self, ctx: GuardContext, call: ToolCall, outcome: Outcome) -> None:
        st: _RunState = self.state(ctx, lambda: _RunState(ctx))
        claim = st.claims.pop(call.tool_use_id, None)
        if claim is None:
            return
        rid, fence = claim
        with _scope(ctx), warnings.catch_warnings():
            warnings.simplefilter("ignore")
            if outcome.status == "succeeded":
                st.ledger.complete(rid, outcome.text, expected_fence=fence)
            else:
                exc = TimeoutError(outcome.text) if outcome.status == "timeout" else RuntimeError(outcome.text)
                st.ledger.fail(rid, exc, failed_after_effect=CLASSES[call.name] != SideEffectClass.READ,
                               expected_fence=fence)


def user_turn_number(messages: list) -> int:
    """How many user-authored messages (not tool results) the history has so far."""
    return sum(1 for m in messages if m.get("role") == "user" and isinstance(m.get("content"), str))


class MyceliumTurnkeyGuard(MyceliumGuard):
    """Same wiring as "mycelium", plus the host-owned business request_id its docs ask for.

    Our host key: request_id = f"{run_id}:user_turn_{N}:{tool}", N = the user message
    that led to the call (a forced call uses the turn it was injected after). One
    effect per tool per user message. Reads keep the derived identity (a fixed
    request_id would replay a stale read within the turn).
    """

    name = "mycelium_turnkey"

    def call_kwargs(self, ctx: GuardContext, call: ToolCall) -> dict:
        kwargs = super().call_kwargs(ctx, call)
        if CLASSES[call.name] != SideEffectClass.READ:
            kwargs["request_id"] = f"{ctx.run_id}:user_turn_{user_turn_number(ctx.messages)}:{call.name}"
        return kwargs
