"""DoubleTake quickstart: a charge tool whose first reply is lost, then the agent retries.

Runs offline (RulesJudge, no API key):   python examples/quickstart.py
"""

import asyncio

from doubletake import DoubleTake, Execute, Outcome, ReadBack, RulesJudge

LEDGER: list[dict] = []  # stands in for the payment provider


def charge_card(customer: str, amount: float, note: str = "") -> str:
    LEDGER.append({"customer": customer, "amount": amount, "note": note})
    return f"charged {customer} ${amount:.2f}"


def find_charge(effect) -> ReadBack:  # optional read-back: did the unresolved charge happen?
    t = effect.target_dict
    hit = [c for c in LEDGER if c["customer"] == t["customer"] and round(c["amount"] * 100) == t["amount"]]
    return ReadBack("found", hit[-1]) if hit else ReadBack("absent")


guard = DoubleTake(judge=RulesJudge())  # DoubleTake() uses the claude-haiku-5-5 intent judge
guard.register_tool("charge_card", target_fields=("customer", "amount"), read_back=find_charge,
                    read_back_name="the payment ledger",  # amounts in targets are cents
                    describe=lambda tool, t: f"charge customer {dict(t)['customer']} ${dict(t)['amount'] / 100:.2f}")


async def guarded_charge(conversation: str, user_messages: list[str], args: dict, lose_reply=False) -> str:
    decision = await guard.check(conversation, user_messages, "charge_card", args)
    if not isinstance(decision, Execute):  # ReturnResult / Block / AskUser: hand this text to the model
        return getattr(decision, "text", None) or getattr(decision, "message", None) or decision.question
    result = charge_card(**args)
    if lose_reply:  # the charge happened, but the agent never hears back
        await guard.record(conversation, "charge_card", args, Outcome("timeout"))
        return "timeout: no response"
    await guard.record(conversation, "charge_card", args, Outcome("succeeded", result))
    return result


async def main() -> None:
    users = ["Charge customer 881 $50"]
    print("1st call:", await guarded_charge("conv-1", users, {"customer": "881", "amount": 50}, lose_reply=True))
    print("retry:   ", await guarded_charge("conv-1", users, {"customer": "881", "amount": 50, "note": "retry"}))
    print("charges in the ledger:", len(LEDGER))


if __name__ == "__main__":
    asyncio.run(main())
