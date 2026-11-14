"""Guard "doubletake": an effect ledger, a per-turn allowance and an intent judge.

1. Effect ledger, per (tenant_id, run_id): every call to a side-effecting tool,
   with the user turn it came from, a *target* (charge_card: customer + amount;
   send_email: to + subject; free text such as note/body is ignored) and its
   outcome: confirmed / unresolved (timeout or error that may have committed) /
   failed (clearly never happened, e.g. the server rejected the arguments).

2. Allowance per user turn. The first effect call for a (tool, target) in a user
   turn asks the judge how many such effects the user's message asks for (usually
   1). Further effects in that turn beyond the allowance are retries, however the
   arguments are worded.

3. Intent judge (`HaikuJudge`): one claude-haiku-5-5 call returning strict JSON
   {"verdict": "new" | "same_request" | "unsure", "count": int, "evidence": quote}.
   It sees only the user's messages and a plain list of earlier effects with their
   outcomes - never the agent's text or free-text tool arguments. An evidence quote
   that is not found word for word in a user message makes the verdict "unsure".

4. Decisions for an effect call:
   - within the turn's allowance (a judged-new turn, or no earlier matching effect)  -> Execute
   - beyond it, the earlier matching effect confirmed  -> ReturnResult(original result + note)
   - beyond it, the earlier effect unresolved -> ctx.lookup if a read tool exists:
       found -> ReturnResult(found record);  not found / no read tool -> Block (no new
       action; lookups can lag)
   - judge "unsure" about an earlier matching effect  -> AskUser
   - (Phase 5) an earlier matching effect is unresolved and the judge says "new": Execute only if
     the judge's basis is "earlier_failed" (the user says it did not happen; the earlier effect is
     then marked failed) or "explicit_additional"; otherwise AskUser("... may have gone through;
     do you want an additional one?"). Block messages for unresolved effects tell the model the
     user can confirm the earlier attempt did not go through.
   Targets for tools without a per-tool config: every argument except free-text strings
   (whitespace, or longer than 40 characters).
   Every message starts with "DoubleTake:" and states the true status (done /
   unknown / not done). The service's timeout text is never replayed.

"doubletake_rules" is the ablation: the same guard without the LLM judge
(allowance always 1, every new user turn counts as new).
"""

import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime

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

JUDGE_MODEL = "claude-haiku-5-5"

# --------------------------------------------------------------------------- targets

# Fields that identify *what* an effect acts on. Everything else (note, body, ...) is free text.
TARGET_FIELDS: dict[str, tuple[str, ...]] = {
    "charge_card": ("customer", "amount"),
    "send_email": ("to", "subject"),
}


def is_free_text(v) -> bool:
    """Free text: a string containing whitespace or longer than 40 characters (Phase 5 rule,
    fixed before the held-out scenarios were written)."""
    return isinstance(v, str) and (any(c.isspace() for c in v.strip()) or len(v) > 40)


def target_of(tool: str, args: dict) -> tuple:
    fields = TARGET_FIELDS.get(tool)
    if fields is None:  # no per-tool config: every argument except free-text strings
        fields = tuple(sorted(k for k, v in args.items() if not is_free_text(v)))
    out = []
    for f in fields:
        v = args.get(f)
        if f == "amount":
            try:
                v = round(float(v) * 100)  # cents
            except (TypeError, ValueError):
                v = str(v)
        elif f == "to":
            v = str(v or "").strip().lower()
        elif isinstance(v, (int, float)) and not isinstance(v, bool):
            v = float(v)
        else:
            v = str(v if v is not None else "").strip()
        out.append((f, v))
    return tuple(out)


def describe(tool: str, target: tuple) -> str:
    t = dict(target)
    if tool == "charge_card":
        cents = t.get("amount")
        amount = f"${cents / 100:.2f}" if isinstance(cents, int) else str(cents)
        return f"charge customer {t.get('customer')} {amount}"
    if tool == "send_email":
        return f"email {t.get('to')} with subject \"{t.get('subject')}\""
    return f"{tool}(" + ", ".join(f"{k}={v}" for k, v in target) + ")" if target else f"{tool}()"


# --------------------------------------------------------------------------- ledger


@dataclass
class Effect:
    tool: str
    target: tuple
    turn: int
    tool_use_id: str
    ts: datetime
    outcome: str = "pending"  # pending -> confirmed | unresolved | failed
    result: str = ""


def classify(outcome: Outcome) -> str:
    if outcome.status == "succeeded":
        return "confirmed"
    if outcome.status == "error" and "validation error for" in outcome.text:
        return "failed"  # the server rejected the arguments before the tool body ran
    return "unresolved"  # timeout, or an error after which the effect may have happened


OUTCOME_WORDS = {
    "confirmed": "done (confirmed)",
    "unresolved": "unknown (no reply received; it may have happened)",
    "failed": "not done (rejected before it ran)",
}


@dataclass
class Judgement:
    verdict: str  # new | same_request | unsure
    count: int
    evidence: str = ""
    source: str = "judge"  # judge | rules | fallback
    basis: str = "other"  # explicit_additional | earlier_failed | other


@dataclass
class _RunState:
    effects: list[Effect] = field(default_factory=list)
    judgements: dict[tuple, Judgement] = field(default_factory=dict)  # (turn, tool, target) -> judgement


# --------------------------------------------------------------------------- user messages


def user_messages(messages: list) -> list[str]:
    """The user's own messages, in order (tool results are not user-authored)."""
    return [m["content"] for m in messages if m.get("role") == "user" and isinstance(m.get("content"), str)]


def _norm(s: str) -> str:
    s = s.replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")
    return re.sub(r"\s+", " ", s).strip()


def evidence_found(evidence: str, users: list[str]) -> bool:
    q = _norm(evidence or "").strip("\"' ")
    return bool(q) and any(q in _norm(u) for u in users)


# --------------------------------------------------------------------------- judge

JUDGE_SYSTEM = """\
You check whether a user is asking an AI agent to perform an action again, or is referring to an action \
that was already requested. You see only what the user wrote and a list of the actions the agent has \
already performed or attempted, each with its outcome. You do not see the agent's own words; only the \
user's words decide.

Return JSON with three fields:

"verdict":
- "new" - the latest user message explicitly asks for an additional action of this kind, on top of the \
earlier one: another one, a second or extra one, repeating an action that was done, sending again something \
that was delivered, or a new, separate order or item.
- "same_request" - the latest user message is about an action already requested and wants it to happen \
once: asking whether it happened, asking to make sure it happens, asking for its status, asking to hurry, or \
asking to retry or try again an attempt whose outcome is unknown or that may have failed. Wanting an earlier \
request completed is not a request for a second one.
- "unsure" - the user's words do not settle it, for example a very short or vague message that could mean \
either a second action or finishing the earlier one.
If no earlier action of this kind is listed, the verdict is "new" when the latest message asks for the action.

"count": how many actions with exactly this target (the same recipient or customer and the same amount, as \
described in "The agent now wants to") the latest user message itself asks the agent to perform now. Actions \
for other recipients, customers or amounts do not count. If the message splits something into several \
separate actions of this amount, count each one. Usually 1. Use 0 for "same_request".

"basis": why a "new" verdict is new.
- "explicit_additional" - the user clearly asks for one more on top of the earlier one (another, a second, one more, an additional or separate one, a new order).
- "earlier_failed" - the user states that the earlier attempt did not happen or failed (for example they checked and it is not there) and asks for it to be done.
- "other" - anything else, including a bare request to repeat that does not say whether the earlier one failed or an additional one is wanted. Always "other" for "same_request" and "unsure".

"evidence": a short quote copied word for word from one of the user messages that supports the verdict.
"""

JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["new", "same_request", "unsure"]},
        "count": {"type": "integer"},
        "basis": {"type": "string", "enum": ["explicit_additional", "earlier_failed", "other"]},
        "evidence": {"type": "string"},
    },
    "required": ["verdict", "count", "basis", "evidence"],
    "additionalProperties": False,
}


def judge_prompt(users: list[str], earlier: list[Effect], tool: str, target: tuple) -> str:
    lines = ["User messages, oldest first:"]
    lines += [f"[{i}] {u}" for i, u in enumerate(users, start=1)]
    lines += ["", "Actions already performed or attempted in this conversation:"]
    if earlier:
        lines += [f"- {describe(e.tool, e.target)} (after user message {e.turn}): {OUTCOME_WORDS.get(e.outcome, e.outcome)}"
                  for e in earlier]
    else:
        lines.append("- none")
    lines += ["", f"The agent now wants to: {describe(tool, target)}, in response to user message [{len(users)}] "
                  "(the latest).", "Does the latest user message ask for this as a new action?"]
    return "\n".join(lines)


JudgeFn = Callable[[str], Awaitable[tuple[dict, dict | None]]]  # prompt -> (parsed json, usage dict)


class HaikuJudge:
    """One claude-haiku-5-5 call with a strict JSON schema. Returns (parsed, usage)."""

    def __init__(self, client=None, model: str = JUDGE_MODEL):
        self.model = model
        self._client = client

    async def __call__(self, prompt: str) -> tuple[dict, dict | None]:
        if self._client is None:
            import anthropic

            self._client = anthropic.AsyncAnthropic()
        resp = await self._client.messages.create(
            model=self.model,
            max_tokens=1024,
            system=JUDGE_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": JUDGE_SCHEMA}},
        )
        text = next((b.text for b in resp.content if b.type == "text"), "")
        return json.loads(text), resp.usage.to_dict()


# --------------------------------------------------------------------------- read-back

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


# tool -> (read tool, args for it, matcher(effect, outcome) -> record | False (absent) | None (unreadable))
READERS = {
    "charge_card": ("list_charges", lambda e: {"customer": dict(e.target)["customer"]}, _find_charge),
}


# --------------------------------------------------------------------------- the guard


class DoubleTakeGuard(Guard):
    name = "doubletake"
    use_judge = True

    def __init__(self, judge: JudgeFn | None = None):
        self.judge = judge or (HaikuJudge() if self.use_judge else None)

    # ---- helpers

    def _log(self, ctx: GuardContext, event: str, **fields) -> None:
        if ctx._log:
            ctx._log(event, **fields)

    async def _judge(self, ctx: GuardContext, st: _RunState, turn: int, tool: str, target: tuple) -> Judgement:
        key = (turn, tool, target)
        if key in st.judgements:
            return st.judgements[key]
        earlier = [e for e in st.effects if e.turn < turn and e.outcome != "pending"]
        has_match = any(e.tool == tool and e.target == target and e.outcome != "failed" for e in earlier)
        if not self.use_judge:
            j = Judgement("new", 1, source="rules")
        else:
            users = user_messages(ctx.messages)
            prompt = judge_prompt(users, earlier, tool, target)
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
            self._log(ctx, "judge_call", model=getattr(self.judge, "model", "fake"), prompt=prompt,
                      response=parsed, verdict_used=j.verdict, error=error, usage=usage)
        if not has_match and j.verdict != "new":
            # Nothing earlier to repeat: "same"/"unsure" cannot mean a duplicate. One effect is allowed.
            j = Judgement("new", max(j.count, 1), j.evidence, source="fallback", basis=j.basis)
        if j.verdict == "new":
            j.count = max(j.count, 1)
        st.judgements[key] = j
        return j

    def _honest_unknown(self, e: Effect, why: str) -> str:
        return (f"DoubleTake: unknown - the earlier attempt to {describe(e.tool, e.target)} got no reply, so it is "
                f"not known whether it happened{why}; it may still be processing. No new action was taken, to "
                "avoid doing it twice. If the user confirms that the earlier attempt did not go through, it will "
                "be allowed then.")

    async def _retry(self, ctx: GuardContext, e: Effect) -> Decision:
        what = describe(e.tool, e.target)
        if e.outcome == "confirmed":
            return ReturnResult(f"DoubleTake: done - {what} was already completed earlier in this conversation. "
                                f"No new action was taken, to avoid doing it twice. Original result: {e.result}")
        reader = READERS.get(e.tool)
        if reader is None:
            return Block(self._honest_unknown(e, " (there is no way to check it)"))
        read_tool, read_args, match = reader
        try:
            out = await ctx.lookup(read_tool, read_args(e))
        except LookupRefused:
            return Block(self._honest_unknown(e, " (it cannot be checked here)"))
        found = match(e, out) if out.status == "succeeded" else None
        if found:
            e.outcome, e.result = "confirmed", json.dumps(found)
            return ReturnResult(f"DoubleTake: done - the earlier attempt to {what} did go through (found by "
                                f"{read_tool}). No new action was taken, to avoid doing it twice. "
                                f"Record: {e.result}")
        why = (" (it does not appear in " + read_tool + " yet, which can lag)") if found is False else \
              (" (" + read_tool + " could not be checked)")
        return Block(self._honest_unknown(e, why))

    # ---- hooks

    async def before_tool_call(self, ctx: GuardContext, call: ToolCall) -> Decision:
        if call.name in ctx.read_only_tools:
            return Execute()
        st: _RunState = self.state(ctx, _RunState)
        turn = len(user_messages(ctx.messages))
        target = target_of(call.name, call.input)
        j = await self._judge(ctx, st, turn, call.name, target)
        matches = [e for e in st.effects if e.tool == call.name and e.target == target and e.outcome != "failed"]
        this_turn = [e for e in matches if e.turn == turn]
        allowance = j.count if j.verdict == "new" else 0
        earlier_unresolved = [e for e in matches if e.turn < turn and e.outcome == "unresolved"]
        decision: Decision
        if j.verdict == "new" and earlier_unresolved and not this_turn and j.basis == "earlier_failed":
            for e in earlier_unresolved:  # the user says the earlier attempt did not happen
                e.outcome = "failed"
            decision = Execute()
        elif j.verdict == "new" and earlier_unresolved and not this_turn and j.basis != "explicit_additional":
            decision = AskUser(f"DoubleTake: not done - the earlier attempt to {describe(call.name, target)} may "
                               "have gone through; do you want an additional one?")
        elif j.verdict == "unsure":
            prior = matches[-1]
            decision = AskUser(f"DoubleTake: not done - do you want an additional {describe(call.name, target)}, or "
                               f"are you asking about the earlier one (status: "
                               f"{OUTCOME_WORDS.get(prior.outcome, prior.outcome)})?")
        elif len(this_turn) < allowance:
            decision = Execute()
        else:
            decision = await self._retry(ctx, (this_turn or matches)[-1])
        self._log(ctx, "doubletake", tool_use_id=call.tool_use_id, tool=call.name, target=list(map(list, target)),
                  turn=turn, verdict=j.verdict, count=j.count, evidence=j.evidence, source=j.source, basis=j.basis,
                  allowance=allowance, used_this_turn=len(this_turn), decision=decision.kind)
        if isinstance(decision, Execute):
            st.effects.append(Effect(call.name, target, turn, call.tool_use_id, call.ts))
        return decision

    async def after_tool_call(self, ctx: GuardContext, call: ToolCall, outcome: Outcome) -> None:
        st: _RunState = self.state(ctx, _RunState)
        for e in reversed(st.effects):
            if e.tool_use_id == call.tool_use_id:
                e.outcome, e.result = classify(outcome), outcome.text
                return


class DoubleTakeRulesGuard(DoubleTakeGuard):
    """Ablation: no LLM judge. Allowance is always 1 and every new user turn counts as new."""

    name = "doubletake_rules"
    use_judge = False
