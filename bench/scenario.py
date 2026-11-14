"""Scenario files (scenarios/NN_<id>.yaml, run in file-name order) and their schema.

    id: truthful_lookup
    description: ...
    category: retry              # retry: right answer is ONE effect; intentional: as many as asked for
    faults: {lose_reply: {...}}  # same JSON shape as kitchen/faults.json
    exclude_tools: []            # hidden from the model
    turns:                       # scripted user messages, sent in order
      - charge customer 881 $50
      - text: Did that go through?
        if_effects_below: 2      # send only if the effect table has fewer rows than this
    forced:                      # optional: inject an assistant tool call after scripted turn K
      after_turn: 1
      tool: charge_card
      input: {customer: "881", amount: 50.0, note: retry after timeout}
      same_as_last: false        # true: reuse the input of the last executed call to `tool`
    on_ask: "..."                # optional: simulated user reply when the agent asks
    expect:
      table: charges             # charges | emails
      effects: 1                 # exact row count expected at the end
      ask_ok: false              # retry cases: asking the user instead is acceptable
    requires_fault_hit: true     # the run is INVALID if the lost reply never fired
"""

import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ROOT = Path(__file__).resolve().parent.parent
SCENARIO_DIR = ROOT / "scenarios"
HELDOUT_DIR = SCENARIO_DIR / "heldout"

EFFECT_TOOLS = {"charges": "charge_card", "emails": "send_email", "refunds": "issue_refund"}
# Kitchen tools added in Phase 5. Hidden unless a scenario lists them in extra_tools, so the
# Phase 2-4 scenarios see exactly the tools they always saw.
OPT_IN_TOOLS = ("issue_refund", "list_refunds")


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Turn(Strict):
    text: str = Field(min_length=1)
    if_effects_below: int | None = Field(default=None, ge=1)


class Forced(Strict):
    after_turn: int = Field(ge=1)
    tool: str
    input: dict
    same_as_last: bool = False


class Expect(Strict):
    table: Literal["charges", "emails", "refunds"]
    effects: int = Field(ge=0)
    ask_ok: bool = False


class Scenario(Strict):
    id: str = Field(pattern=r"^[a-z0-9_]+$")
    description: str
    category: Literal["retry", "intentional"]
    faults: dict = Field(default_factory=dict)
    exclude_tools: tuple[str, ...] = ()
    extra_tools: tuple[str, ...] = ()  # opt-in kitchen tools this scenario may use
    turns: tuple[Turn, ...] = Field(min_length=1)
    forced: Forced | None = None
    on_ask: str | None = None
    expect: Expect
    requires_fault_hit: bool = False

    @field_validator("turns", mode="before")
    @classmethod
    def _plain_strings_are_turns(cls, v):
        return [{"text": t} if isinstance(t, str) else t for t in v]

    @model_validator(mode="after")
    def _consistent(self):
        if self.forced and self.forced.after_turn > len(self.turns):
            raise ValueError(f"forced.after_turn={self.forced.after_turn} but only {len(self.turns)} turns")
        if self.expect.ask_ok and self.category != "retry":
            raise ValueError("ask_ok only applies to retry cases")
        if self.requires_fault_hit and not self.lose_reply_tools:
            raise ValueError("requires_fault_hit needs an enabled lose_reply fault")
        bad = set(self.extra_tools) - set(OPT_IN_TOOLS)
        if bad:
            raise ValueError(f"extra_tools may only name opt-in tools {OPT_IN_TOOLS}: {sorted(bad)}")
        if self.effect_tool in OPT_IN_TOOLS and self.effect_tool not in self.extra_tools:
            raise ValueError(f"expect.table {self.expect.table} needs {self.effect_tool} in extra_tools")
        unknown = set(self.faults) - {"lose_reply", "pending", "lookup_fails"}
        if unknown:
            raise ValueError(f"unknown fault(s): {sorted(unknown)}")
        return self

    @property
    def hidden_tools(self) -> tuple[str, ...]:
        """Tools the model does not get: explicit exclusions plus opt-in tools not requested."""
        return tuple(dict.fromkeys(self.exclude_tools + tuple(t for t in OPT_IN_TOOLS if t not in self.extra_tools)))

    @property
    def effect_tool(self) -> str:
        return EFFECT_TOOLS[self.expect.table]

    @property
    def lose_reply_tools(self) -> tuple[str, ...]:
        lr = self.faults.get("lose_reply") or {}
        return tuple(lr.get("tools", [])) if lr.get("enabled") else ()


def load_scenario(path: Path) -> Scenario:
    sc = Scenario.model_validate(yaml.safe_load(Path(path).read_text()))
    if sc.id != re.sub(r"^\d+_", "", Path(path).stem):
        raise ValueError(f"{path}: id {sc.id!r} does not match the file name")
    return sc


def load_scenarios(directory: Path = SCENARIO_DIR, ids: list[str] | None = None,
                   include_heldout: bool = False) -> list[Scenario]:
    """Scenarios in file-name order (files are numbered), optionally filtered by id.

    The default is the 12 main scenarios; include_heldout=True appends scenarios/heldout/."""
    paths = sorted(Path(directory).glob("*.yaml"))
    if include_heldout:
        paths += sorted(HELDOUT_DIR.glob("*.yaml"))
    found = [load_scenario(p) for p in paths]
    dupes = {s.id for s in found if sum(t.id == s.id for t in found) > 1}
    if dupes:
        raise ValueError(f"duplicate scenario id(s): {sorted(dupes)}")
    if ids:
        by_id = {s.id: s for s in found}
        missing = [i for i in ids if i not in by_id]
        if missing:
            raise ValueError(f"unknown scenario(s): {', '.join(missing)}")
        return [by_id[i] for i in ids]
    return found

