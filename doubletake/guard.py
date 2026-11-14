"""DoubleTake: stop an agent from repeating a side effect the user asked for once.

    guard = DoubleTake()                                    # HaikuJudge by default
    guard.register_tool("charge_card", target_fields=("customer", "amount"),
                        read_back=find_charge, read_back_name="list_charges")

    decision = await guard.check(conversation_id, user_messages, "charge_card", args)
    if isinstance(decision, Execute):
        try:
            result = charge_card(**args)
            await guard.record(conversation_id, "charge_card", args, Outcome("succeeded", result))
        except TimeoutError:
            await guard.record(conversation_id, "charge_card", args, Outcome("timeout"))
    else:
        ...  # ReturnResult / Block / AskUser: do not run the tool; hand the text to the model

How a call is decided (per conversation):
  - Every call to a registered tool is recorded with the user turn it came from, a *target* (the
    arguments that identify what it acts on; free text is ignored) and its outcome: confirmed /
    unresolved (timeout or error that may have committed) / failed (rejected before it ran).
  - The first call for a target in a user turn asks the judge how many such effects the user's message
    asks for (usually 1). Calls beyond that allowance are retries, however the arguments are worded.
  - A retry of a confirmed effect gets the original result back (ReturnResult). A retry of an
    unresolved effect is checked with the read-back if there is one: found -> ReturnResult, otherwise
    Block (lookups can lag; nothing new is done).
  - Judge "unsure" about an earlier matching effect -> AskUser. If the earlier matching effect is
    unresolved and the judge says "new", it executes only when the user says the earlier one failed or
    explicitly asks for an additional one; otherwise AskUser.
  - Every message starts with "DoubleTake:" and states the true status: done / unknown / not done.

Unregistered tools always Execute: register only the tools with side effects.
"""

import asyncio
import inspect
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from doubletake.decisions import AskUser, Block, Decision, Execute, Outcome, ReturnResult
from doubletake.judge import (
    OUTCOME_WORDS,
    HaikuJudge,
    Judgement,
    evidence_found,
    judge_prompt,
)
from doubletake.store import ConversationState, Effect, MemoryStore, Store
from doubletake.targets import Describer, generic_description, target_of

DEFAULT_TENANT = "default"


@dataclass(frozen=True)
class ReadBack:
    """Result of checking whether an unresolved effect actually happened."""

    status: Literal["found", "absent", "error", "unavailable"]
    record: dict | None = None  # the found record (for status "found")


ReadBackFn = Callable[[Effect], "ReadBack | Awaitable[ReadBack]"]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    target_fields: tuple[str, ...] | None = None  # None: every argument except free-text strings
    read_back: ReadBackFn | None = None  # how to check an unresolved effect
    read_back_name: str = "read-back"  # used in messages: "found by <name>"
    describe: Describer | None = None  # (tool, target) -> "charge customer 7 $5.00"


def classify(outcome: Outcome) -> str:
    if outcome.status == "succeeded":
        return "confirmed"
    if outcome.status == "error" and "validation error for" in outcome.text:
        return "failed"  # rejected the arguments before the tool body ran
    return "unresolved"  # timeout, or an error after which the effect may have happened


EventFn = Callable[..., None]  # on_event(event_name, **fields)


class DoubleTake:
    def __init__(self, judge=None, store: Store | None = None, on_event: EventFn | None = None):
        self.judge = judge if judge is not None else HaikuJudge()
        self.store: Store = store if store is not None else MemoryStore()
        self.on_event = on_event
        self.tools: dict[str, ToolSpec] = {}

    # ------------------------------------------------------------------ registration

    def register_tool(self, name: str, target_fields: tuple[str, ...] | None = None,
                      read_back: ReadBackFn | None = None, read_back_name: str = "read-back",
                      describe: Describer | None = None) -> ToolSpec:
        spec = ToolSpec(name, tuple(target_fields) if target_fields else None, read_back, read_back_name, describe)
        self.tools[name] = spec
        return spec

    def describe(self, tool: str, target: tuple) -> str:
        spec = self.tools.get(tool)
        return (spec.describe if spec and spec.describe else generic_description)(tool, target)

    def target(self, tool: str, args: dict) -> tuple:
        spec = self.tools.get(tool)
        return target_of(args, spec.target_fields if spec else None)

    # ------------------------------------------------------------------ public API

    async def check(self, conversation_id: str, user_messages: list[str], tool_name: str, args: dict, *,
                    tenant_id: str = DEFAULT_TENANT, call_id: str | None = None,
                    at: datetime | None = None) -> Decision:
        """Decide what to do with a tool call, before running it.

        `user_messages`: the user's own messages in this conversation so far, oldest first (not the
        agent's text, not tool results). `call_id`: optional id to pair with `record`."""
        if tool_name not in self.tools:
            return Execute()
        st = self._load(tenant_id, conversation_id)
        turn = len(user_messages)
        target = self.target(tool_name, args)
        j = await self._judge(st, user_messages, turn, tool_name, target)
        matches = [e for e in st.effects if e.tool == tool_name and e.target == target and e.outcome != "failed"]
        this_turn = [e for e in matches if e.turn == turn]
        allowance = j.count if j.verdict == "new" else 0
        earlier_unresolved = [e for e in matches if e.turn < turn and e.outcome == "unresolved"]
        decision: Decision
        if j.verdict == "new" and earlier_unresolved and not this_turn and j.basis == "earlier_failed":
            for e in earlier_unresolved:  # the user says the earlier attempt did not happen
                e.outcome = "failed"
            decision = Execute()
        elif j.verdict == "new" and earlier_unresolved and not this_turn and j.basis != "explicit_additional":
            decision = AskUser(f"DoubleTake: not done - the earlier attempt to {self.describe(tool_name, target)} may "
                               "have gone through; do you want an additional one?")
        elif j.verdict == "unsure":
            prior = matches[-1]
            decision = AskUser(f"DoubleTake: not done - do you want an additional {self.describe(tool_name, target)}, "
                               f"or are you asking about the earlier one (status: "
                               f"{OUTCOME_WORDS.get(prior.outcome, prior.outcome)})?")
        elif len(this_turn) < allowance:
            decision = Execute()
        else:
            decision = await self._retry((this_turn or matches)[-1])
        self._emit("doubletake", tool_use_id=call_id, tool=tool_name, target=list(map(list, target)),
                   turn=turn, verdict=j.verdict, count=j.count, evidence=j.evidence, source=j.source, basis=j.basis,
                   allowance=allowance, used_this_turn=len(this_turn), decision=decision.kind)
        if isinstance(decision, Execute):
            st.effects.append(Effect(tool_name, target, turn, call_id, at or datetime.now(timezone.utc)))
        self.store.save(tenant_id, conversation_id, st)
        return decision

    async def record(self, conversation_id: str, tool_name: str, args: dict, outcome: Outcome | str,
                     text: str = "", *, tenant_id: str = DEFAULT_TENANT, call_id: str | None = None) -> None:
        """Report what happened after an executed call: Outcome("succeeded" | "error" | "timeout", text)."""
        if tool_name not in self.tools:
            return
        if isinstance(outcome, str):
            outcome = Outcome(outcome, text)  # type: ignore[arg-type]
        st = self._load(tenant_id, conversation_id)
        target = self.target(tool_name, args)
        for e in reversed(st.effects):
            same = e.tool_use_id == call_id if call_id is not None else \
                (e.tool == tool_name and e.target == target and e.outcome == "pending")
            if same:
                e.outcome, e.result = classify(outcome), outcome.text
                break
        self.store.save(tenant_id, conversation_id, st)

    def effects(self, conversation_id: str, *, tenant_id: str = DEFAULT_TENANT) -> list[Effect]:
        """The recorded effects of a conversation (for inspection and tests)."""
        st = self.store.load(tenant_id, conversation_id)
        return list(st.effects) if st else []

    # ------------------------------------------------------------------ internals

    def _load(self, tenant_id: str, conversation_id: str) -> ConversationState:
        st = self.store.load(tenant_id, conversation_id)
        return st if st is not None else ConversationState()

    def _emit(self, event: str, **fields) -> None:
        if self.on_event:
            self.on_event(event, **fields)

    async def _judge(self, st: ConversationState, users: list[str], turn: int, tool: str, target: tuple) -> Judgement:
        key = (turn, tool, target)
        if key in st.judgements:
            return st.judgements[key]
        earlier = [e for e in st.effects if e.turn < turn and e.outcome != "pending"]
        has_match = any(e.tool == tool and e.target == target and e.outcome != "failed" for e in earlier)
        if not getattr(self.judge, "uses_llm", True):
            j = Judgement("new", 1, source="rules")
        else:
            prompt = judge_prompt(users, earlier, tool, target, self.describe)
            try:
                parsed, usage = await self.judge(prompt)
                verdict = parsed.get("verdict")
                count = int(parsed.get("count", 0))
                evidence = str(parsed.get("evidence", ""))
                basis = parsed.get("basis", "other")
                error = None
            except Exception as e:  # judge unavailable or malformed: treat as unsure
                parsed, usage, verdict, count, evidence, error = {}, None, "unsure", 0, "", f"{type(e).__name__}: {e}"
                basis = "other"
            if basis not in ("explicit_additional", "earlier_failed", "other"):
                basis = "other"
            if verdict not in ("new", "same_request", "unsure"):
                verdict = "unsure"
            if verdict != "unsure" and not evidence_found(evidence, users):
                verdict = "unsure"  # a quote that is not in the user's words decides nothing
            j = Judgement(verdict, max(0, min(count, 20)), evidence, basis=basis if verdict == "new" else "other")
            self._emit("judge_call", model=getattr(self.judge, "model", "fake"), prompt=prompt,
                       response=parsed, verdict_used=j.verdict, error=error, usage=usage)
        if not has_match and j.verdict != "new":
            # Nothing earlier to repeat: "same"/"unsure" cannot mean a duplicate. One effect is allowed.
            j = Judgement("new", max(j.count, 1), j.evidence, source="fallback", basis=j.basis)
        if j.verdict == "new":
            j.count = max(j.count, 1)
        st.judgements[key] = j
        return j

    def _honest_unknown(self, e: Effect, why: str) -> str:
        return (f"DoubleTake: unknown - the earlier attempt to {self.describe(e.tool, e.target)} got no reply, so it "
                f"is not known whether it happened{why}; it may still be processing. No new action was taken, to "
                "avoid doing it twice. If the user confirms that the earlier attempt did not go through, it will "
                "be allowed then.")

    async def _retry(self, e: Effect) -> Decision:
        what = self.describe(e.tool, e.target)
        if e.outcome == "confirmed":
            return ReturnResult(f"DoubleTake: done - {what} was already completed earlier in this conversation. "
                                f"No new action was taken, to avoid doing it twice. Original result: {e.result}")
        spec = self.tools.get(e.tool)
        if spec is None or spec.read_back is None:
            return Block(self._honest_unknown(e, " (there is no way to check it)"))
        rb = spec.read_back(e)
        if inspect.isawaitable(rb):
            rb = await rb
        name = spec.read_back_name
        if rb.status == "unavailable":
            return Block(self._honest_unknown(e, " (it cannot be checked here)"))
        if rb.status == "found" and rb.record:
            e.outcome, e.result = "confirmed", json.dumps(rb.record)
            return ReturnResult(f"DoubleTake: done - the earlier attempt to {what} did go through (found by "
                                f"{name}). No new action was taken, to avoid doing it twice. "
                                f"Record: {e.result}")
        why = (" (it does not appear in " + name + " yet, which can lag)") if rb.status == "absent" else \
              (" (" + name + " could not be checked)")
        return Block(self._honest_unknown(e, why))

    # ------------------------------------------------------------------ sync convenience

    def check_sync(self, *a, **kw) -> Decision:
        return asyncio.run(self.check(*a, **kw))

    def record_sync(self, *a, **kw) -> None:
        asyncio.run(self.record(*a, **kw))
