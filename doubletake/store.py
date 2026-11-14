"""Per-conversation state, keyed by (tenant_id, conversation_id), behind a small Store interface.

`MemoryStore` keeps state in process memory. A Redis or SQL store implements the same two methods;
`ConversationState.to_dict()` / `from_dict()` give a JSON-safe form to persist.
"""

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Protocol

from doubletake.judge import Judgement


@dataclass
class Effect:
    """One executed (or blocked-from-retry) side-effecting call, as the guard remembers it."""

    tool: str
    target: tuple
    turn: int  # number of user messages when the call was made
    tool_use_id: str | None
    ts: datetime  # when the call was checked
    outcome: str = "pending"  # pending -> confirmed | unresolved | failed
    result: str = ""

    @property
    def target_dict(self) -> dict:
        return dict(self.target)


@dataclass
class ConversationState:
    effects: list[Effect] = field(default_factory=list)
    judgements: dict[tuple, Judgement] = field(default_factory=dict)  # (turn, tool, target) -> judgement

    def to_dict(self) -> dict:
        return {
            "effects": [{**asdict(e), "target": [list(p) for p in e.target], "ts": e.ts.isoformat()}
                        for e in self.effects],
            "judgements": [{"key": [k[0], k[1], [list(p) for p in k[2]]], **asdict(j)}
                           for k, j in self.judgements.items()],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ConversationState":
        st = cls()
        for e in d.get("effects", []):
            st.effects.append(Effect(**{**e, "target": tuple(tuple(p) for p in e["target"]),
                                        "ts": datetime.fromisoformat(e["ts"])}))
        for j in d.get("judgements", []):
            turn, tool, target = j.pop("key")
            st.judgements[(turn, tool, tuple(tuple(p) for p in target))] = Judgement(**j)
        return st


class Store(Protocol):
    def load(self, tenant_id: str, conversation_id: str) -> ConversationState | None: ...

    def save(self, tenant_id: str, conversation_id: str, state: ConversationState) -> None: ...


class MemoryStore:
    """In-process store. State is lost when the process exits."""

    def __init__(self) -> None:
        self._data: dict[tuple[str, str], ConversationState] = {}

    def load(self, tenant_id: str, conversation_id: str) -> ConversationState | None:
        return self._data.get((tenant_id, conversation_id))

    def save(self, tenant_id: str, conversation_id: str, state: ConversationState) -> None:
        self._data[(tenant_id, conversation_id)] = state
