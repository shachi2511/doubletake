"""DoubleTake: stop AI agents from repeating side effects (double charges, duplicate emails).

    from doubletake import DoubleTake, Execute, Outcome
    guard = DoubleTake()                       # HaikuJudge; DoubleTake(judge=RulesJudge()) needs no API key
    guard.register_tool("charge_card", target_fields=("customer", "amount"))
    decision = await guard.check(conversation_id, user_messages, "charge_card", args)
    ...
    await guard.record(conversation_id, "charge_card", args, Outcome("succeeded", result_text))
"""

from doubletake.decisions import AskUser, Block, Decision, Execute, Outcome, ReturnResult
from doubletake.guard import DEFAULT_TENANT, DoubleTake, ReadBack, ToolSpec
from doubletake.judge import JUDGE_MODEL, HaikuJudge, Judgement, RulesJudge
from doubletake.store import ConversationState, Effect, MemoryStore, Store
from doubletake.targets import is_free_text, target_of

__version__ = "0.1.0"

__all__ = [
    "DEFAULT_TENANT", "JUDGE_MODEL", "AskUser", "Block", "ConversationState", "Decision", "DoubleTake", "Effect",
    "Execute", "HaikuJudge", "Judgement", "MemoryStore", "Outcome", "ReadBack", "ReturnResult", "RulesJudge",
    "Store", "ToolSpec", "is_free_text", "target_of",
]
