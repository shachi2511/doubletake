"""Guard registry: `--guard <name>` picks one. Phases 3-4 register more here."""

from agent.guards.base import (
    NEEDS_CONFIRMATION,
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
from agent.guards.doubletake import DoubleTakeGuard, DoubleTakeRulesGuard
from agent.guards.exact_hash import ExactHashGuard
from agent.guards.none import NoneGuard
from agent.guards.verify_before_retry import VerifyBeforeRetryGuard

GUARDS: dict[str, type[Guard]] = {
    NoneGuard.name: NoneGuard,
    ExactHashGuard.name: ExactHashGuard,
    VerifyBeforeRetryGuard.name: VerifyBeforeRetryGuard,
    DoubleTakeGuard.name: DoubleTakeGuard,
    DoubleTakeRulesGuard.name: DoubleTakeRulesGuard,
}

# Third-party baselines (requirements-baselines.txt): registered only if installed.
try:
    from agent.guards.agent_ledger_guard import AgentLedgerGuard

    GUARDS[AgentLedgerGuard.name] = AgentLedgerGuard
except ImportError:  # pragma: no cover
    pass
try:
    from agent.guards.mycelium_guard import MyceliumGuard, MyceliumTurnkeyGuard

    GUARDS[MyceliumGuard.name] = MyceliumGuard
    GUARDS[MyceliumTurnkeyGuard.name] = MyceliumTurnkeyGuard
except ImportError:  # pragma: no cover
    pass


def get_guard(name: str) -> Guard:
    try:
        return GUARDS[name]()
    except KeyError:
        raise ValueError(f"unknown guard {name!r}; known: {', '.join(sorted(GUARDS))}") from None


__all__ = [
    "GUARDS", "NEEDS_CONFIRMATION", "AskUser", "Block", "Decision", "Execute", "Guard", "GuardContext", "LookupRefused",
    "NoneGuard", "Outcome", "ReturnResult", "ToolCall", "get_guard",
]
