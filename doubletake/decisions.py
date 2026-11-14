"""What the guard tells you to do with a tool call, and what you report back afterwards."""

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class Execute:
    """Run the tool."""

    kind: Literal["execute"] = "execute"


@dataclass(frozen=True)
class ReturnResult:
    """Do not run the tool; give the model `text` as a normal (successful) tool result."""

    text: str
    kind: Literal["return_result"] = "return_result"


@dataclass(frozen=True)
class Block:
    """Do not run the tool; give the model `message` as an error tool result."""

    message: str
    kind: Literal["block"] = "block"


@dataclass(frozen=True)
class AskUser:
    """Do not run the tool; the user must answer `question` first."""

    question: str
    kind: Literal["ask_user"] = "ask_user"


Decision = Execute | ReturnResult | Block | AskUser

OutcomeStatus = Literal["succeeded", "error", "timeout"]


@dataclass(frozen=True)
class Outcome:
    """What happened when an executed tool call ran."""

    status: OutcomeStatus
    text: str = ""
