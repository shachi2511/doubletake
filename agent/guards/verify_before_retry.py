"""Guard "verify_before_retry": our small version of Verified Tool Calls (arXiv 2608.02645).

Only charge_card and send_email are guarded. When an earlier SIMILAR call in this
run ended in timeout or error (charge: same customer and amount; email: same to
and subject), a verifier runs before the new call:

  charge: ctx.lookup("list_charges", customer) for a matching charge created at or
          after the earlier call.
            found          -> ReturnResult(that charge)
            not found      -> wait 3 s (ambiguity budget), check once more;
                              still not found -> Execute (the one allowed retry)
            lookup failed  -> AskUser (also when list_charges is not available)
  email:  no read tool, so the outcome is UNKNOWN -> AskUser.

At most one retry per logical operation: once a retry has been allowed, further
similar calls are blocked. If the earlier similar call SUCCEEDED, nothing special
happens (Execute), as in the paper. The guard cannot read the user's answer to an
AskUser, so it asks again if the model calls again.
"""

import asyncio
import json
from datetime import datetime

from agent.guards.base import (
    AskUser,
    Block,
    Decision,
    Execute,
    Guard,
    GuardContext,
    LookupRefused,
    ReturnResult,
    ToolCall,
)

AMBIGUITY_WAIT_S = 3.0
GUARDED = ("charge_card", "send_email")


def op_key(name: str, args: dict) -> tuple | None:
    if name == "charge_card":
        try:
            cents = round(float(args.get("amount")) * 100)
        except (TypeError, ValueError):
            return None
        return (name, str(args.get("customer", "")).strip(), cents)
    if name == "send_email":
        return (name, str(args.get("to", "")).strip().lower(), str(args.get("subject", "")).strip())
    return None


class VerifyBeforeRetryGuard(Guard):
    name = "verify_before_retry"
    wait_s = AMBIGUITY_WAIT_S

    async def before_tool_call(self, ctx: GuardContext, call: ToolCall) -> Decision:
        if call.name not in GUARDED:
            return Execute()
        key = op_key(call.name, call.input)
        if key is None:
            return Execute()
        failed = [c for c in ctx.calls
                  if c.outcome is not None and c.outcome.status in ("timeout", "error")
                  and op_key(c.name, c.input) == key]
        if not failed:
            return Execute()  # no earlier similar failure (an earlier success changes nothing)
        retried: set = self.state(ctx, set)
        if key in retried:
            return Block("This operation already timed out or failed and was retried once; "
                         "it will not be retried again.")
        if call.name == "send_email":
            return AskUser(f"An earlier email to {call.input.get('to')} with subject "
                           f"\"{call.input.get('subject')}\" got no reply, and there is no way to check "
                           "whether it was sent. Send it again?")

        since = failed[0].ts
        for attempt in (1, 2):
            found = await self._find_charge(ctx, key, since)
            if found is None:
                return AskUser(f"An earlier charge of ${key[2] / 100:.2f} to customer {key[1]} got no reply "
                               "and the charge list could not be checked. Charge again?")
            if found:
                return ReturnResult(json.dumps({**found, "status": "succeeded",
                                                "verified": "found by list_charges after the earlier call timed out"}))
            if attempt == 1:
                await asyncio.sleep(self.wait_s)
        retried.add(key)
        return Execute()

    async def _find_charge(self, ctx: GuardContext, key: tuple, since: datetime) -> dict | None | bool:
        """Matching charge dict, False if verifiably absent, None if the lookup failed."""
        _, customer, cents = key
        try:
            out = await ctx.lookup("list_charges", {"customer": customer})
        except LookupRefused:
            return None
        if out.status != "succeeded":
            return None
        try:
            charges = json.loads(out.text).get("charges", [])
        except ValueError:
            return None
        for c in charges:
            if c.get("amount_cents") == cents and datetime.fromisoformat(c["created_at"]) >= since:
                return c
        return False
