"""The intent judge: is this effect call a new request from the user, or a repeat of an earlier one?

`HaikuJudge` (default) makes one claude-haiku-5-5 call with a strict JSON schema. It sees only the
user's messages and a plain list of earlier effects with their outcomes, never the agent's own text or
free-text tool arguments. `RulesJudge` uses no LLM (every new user message counts as a new request,
one effect per message) for offline use.

A judge is any object with `async __call__(prompt) -> (parsed_json, usage_dict | None)` and a
`model` attribute; set `uses_llm = False` to get the rules behaviour instead.
"""

import json
import re
from dataclasses import dataclass

JUDGE_MODEL = "claude-haiku-5-5"

OUTCOME_WORDS = {
    "confirmed": "done (confirmed)",
    "unresolved": "unknown (no reply received; it may have happened)",
    "failed": "not done (rejected before it ran)",
}

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
- "explicit_additional" - the user clearly asks for one more on top of the earlier one (another, a second, \
one more, an additional or separate one, a new order).
- "earlier_failed" - the user states that the earlier attempt did not happen or failed (for example they \
checked and it is not there) and asks for it to be done.
- "other" - anything else, including a bare request to repeat that does not say whether the earlier one \
failed or an additional one is wanted. Always "other" for "same_request" and "unsure".

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


@dataclass
class Judgement:
    verdict: str  # new | same_request | unsure
    count: int
    evidence: str = ""
    source: str = "judge"  # judge | rules | fallback
    basis: str = "other"  # explicit_additional | earlier_failed | other


def _norm(s: str) -> str:
    s = s.replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")
    return re.sub(r"\s+", " ", s).strip()


def evidence_found(evidence: str, users: list[str]) -> bool:
    """Is the judge's evidence quote in one of the user's messages, word for word (whitespace and quote
    marks normalized)? A quote that is not there decides nothing."""
    q = _norm(evidence or "").strip("\"' ")
    return bool(q) and any(q in _norm(u) for u in users)


def judge_prompt(users: list[str], earlier: list, tool: str, target: tuple, describe) -> str:
    """The judge's input: user messages + earlier effects (description and outcome) + the pending one."""
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


class HaikuJudge:
    """One claude-haiku-5-5 call with a strict JSON schema. Returns (parsed, usage)."""

    uses_llm = True

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


class RulesJudge:
    """No LLM: every new user message counts as a new request for one effect per target.

    Cannot tell "did that go through?" from "charge them another one", and after an unresolved effect
    it always asks the user. Use it offline or as a baseline."""

    uses_llm = False
    model = "rules"
