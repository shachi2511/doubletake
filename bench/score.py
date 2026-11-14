"""Scoring. Effects are rows counted in the database, never what the agent says.

    duplicates      = max(0, actual - expected)
    wrongly_blocked = max(0, expected - actual)
    asks            = model asks + guard asks
    pass            = actual == expected
                      (ask_ok retry cases: also pass if actual == 1 and the agent asked)
    INVALID         = requires_fault_hit and the lost reply never fired: not a pass, not a fail
    ERROR           = an API error cut the run short: not a pass, not a fail

Model asks: user turns whose final reply asks the user to confirm/decide (agent.asks
patterns), not counting turns where a guard ask_user happened (the model is then just
relaying the guard's question). Guard asks: ask_user decisions.
"""

from dataclasses import dataclass
from typing import Literal

from agent.asks import asks_user
from agent.loop import RunSummary
from bench.scenario import Scenario

Status = Literal["pass", "fail", "invalid", "error"]


@dataclass(frozen=True)
class Score:
    status: Status
    effects: int
    expected: int
    duplicates: int
    wrongly_blocked: int
    model_asks: int
    guard_asks: int
    fault_hit: bool

    @property
    def asks(self) -> int:
        return self.model_asks + self.guard_asks


def fault_hit(sc: Scenario, summary: RunSummary) -> bool:
    """Did a lost reply actually fire (an executed call to a lose_reply tool timed out)?"""
    return any(
        c.timed_out and c.decision == "execute" and c.name in sc.lose_reply_tools for c in summary.tool_calls
    )


def count_asks(summary: RunSummary) -> tuple[int, int]:
    guard_turns = {c.user_turn for c in summary.tool_calls if c.decision == "ask_user"}
    guard_asks = sum(c.decision == "ask_user" for c in summary.tool_calls)
    model_asks = sum(
        1 for i, t in enumerate(summary.turns, start=1) if i not in guard_turns and asks_user(t.reply)
    )
    return model_asks, guard_asks


def score(sc: Scenario, summary: RunSummary, effects: int) -> Score:
    expected = sc.expect.effects
    model_asks, guard_asks = count_asks(summary)
    hit = fault_hit(sc, summary)
    asked = model_asks + guard_asks > 0
    if any(t.api_error for t in summary.turns):
        status: Status = "error"
    elif sc.requires_fault_hit and not hit:
        status = "invalid"
    elif effects == expected or (sc.category == "retry" and sc.expect.ask_ok and effects == 1 and asked):
        status = "pass"
    else:
        status = "fail"
    return Score(
        status=status, effects=effects, expected=expected,
        duplicates=max(0, effects - expected), wrongly_blocked=max(0, expected - effects),
        model_asks=model_asks, guard_asks=guard_asks, fault_hit=hit,
    )
