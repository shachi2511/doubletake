"""Guard registry: `--guard <name>` picks one. Phases 3-4 register more here."""

from agent.guards.base import (
    NEEDS_CONFIRMATION,
    AskUser,
    Block,
    Decision,
    Execute,
    Guard,
    GuardContext,
    Outcome,
    ReturnResult,
    ToolCall,
)
from agent.guards.none import NoneGuard

GUARDS: dict[str, type[Guard]] = {
    NoneGuard.name: NoneGuard,
}


def get_guard(name: str) -> Guard:
    try:
        return GUARDS[name]()
    except KeyError:
        raise ValueError(f"unknown guard {name!r}; known: {', '.join(sorted(GUARDS))}") from None


__all__ = [
    "GUARDS", "NEEDS_CONFIRMATION", "AskUser", "Block", "Decision", "Execute", "Guard", "GuardContext",
    "NoneGuard", "Outcome", "ReturnResult", "ToolCall", "get_guard",
]
