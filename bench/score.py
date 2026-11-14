"""Scoring. Effects are rows counted in the database, never what the agent says.

    duplicates      = max(0, actual - expected)
    wrongly_blocked = max(0, expected - actual)
    asks            = model asks + guard asks
    pass            = actual == expected
                      (ask_ok retry cases: also pass if actual == 1 and the agent asked)
    INVALID         = requires_fault_hit and the lost reply never fired: not a pass, not a fail
    ERROR           = an API error cut the run short: not a pass, not a fail

Interventions (measured, never change pass/fail): non-Execute guard decisions
(return_result, block, ask_user) on calls to the scenario's effect tool. A passing
intentional run with interventions > 0 "passed via workaround" (the model got past
the guard, e.g. by changing the note); a clean pass has zero interventions.

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
    interventions: int = 0
    intentional: bool = False

    @property
    def asks(self) -> int:
        return self.model_asks + self.guard_asks

    @property
    def clean_pass(self) -> bool:
        return self.status == "pass" and self.interventions == 0

    @property
    def workaround(self) -> bool:
        """Intentional case that passed although the guard intervened on an effect call."""
        return self.intentional and self.status == "pass" and self.interventions > 0


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


INTERVENTIONS = ("return_result", "block", "ask_user")


def count_interventions(sc: Scenario, summary: RunSummary) -> int:
    return sum(c.name == sc.effect_tool and c.decision in INTERVENTIONS for c in summary.tool_calls)


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
        interventions=count_interventions(sc, summary), intentional=sc.category == "intentional",
    )
