"""Run one scenario once: fresh temp DB + faults file, scripted user, guard in the loop."""

import json
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import anthropic

from agent.asks import asks_user
from agent.guards import get_guard
from agent.loop import DEFAULT_TENANT, AgentSession, RunSummary
from bench.scenario import Scenario
from bench.score import Score, score

MAX_ASK_REPLIES = 3  # per run: stop answering an agent that keeps asking


@dataclass
class RunResult:
    scenario: Scenario
    model: str
    guard: str
    n: int
    summary: RunSummary
    score: Score


def count_effects(db: Path, table: str) -> int:
    if not Path(db).exists():
        return 0
    conn = sqlite3.connect(db)
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    except sqlite3.OperationalError:  # table not created yet
        return 0
    finally:
        conn.close()


def last_turn_asked(summary: RunSummary) -> bool:
    """Did the latest turn end with the agent asking the user (guard ask_user or a model ask)?"""
    if not summary.turns:
        return False
    i = len(summary.turns)
    guard_ask = any(c.user_turn == i and c.decision == "ask_user" for c in summary.tool_calls)
    return guard_ask or bool(asks_user(summary.turns[-1].reply))


def forced_input(sc: Scenario, summary: RunSummary) -> dict:
    f = sc.forced
    if f.same_as_last:
        for c in reversed(summary.tool_calls):
            if c.name == f.tool and c.decision == "execute":
                return dict(c.input)
    return dict(f.input)


async def drive(session: AgentSession, sc: Scenario, db: Path) -> None:
    """Play the scenario's user side against an open session."""
    replies = 0

    async def answer_asks() -> None:
        nonlocal replies
        while sc.on_ask and replies < MAX_ASK_REPLIES and last_turn_asked(session.summary):
            replies += 1
            await session.send(sc.on_ask, source="on_ask")

    for k, turn in enumerate(sc.turns, start=1):
        effects = count_effects(db, sc.expect.table)
        if turn.if_effects_below is not None and effects >= turn.if_effects_below:
            session.trace.log("turn_skipped", turn=k, text=turn.text, effects=effects,
                              if_effects_below=turn.if_effects_below)
        else:
            await session.send(turn.text, source="script")
            await answer_asks()
        if sc.forced and sc.forced.after_turn == k:
            await session.force_tool_call(sc.forced.tool, forced_input(sc, session.summary))
            await answer_asks()


async def run_scenario(
    sc: Scenario,
    model: str,
    guard_name: str,
    n: int,
    *,
    tenant_id: str = DEFAULT_TENANT,
    tool_timeout: float | None = None,
    llm=None,
    tmp_root: Path | None = None,
) -> RunResult:
    with tempfile.TemporaryDirectory(prefix="doubletake-bench-", dir=tmp_root) as tmp:
        db, faults = Path(tmp) / "payments.db", Path(tmp) / "faults.json"
        faults.write_text(json.dumps(sc.faults))
        short = model.removeprefix("claude-")
        run_id = f"p2-{guard_name}-{sc.id}-{short}-run{n}-{datetime.now():%Y%m%d-%H%M%S%f}"
        session = AgentSession(
            model, db, faults, tool_timeout, run_id, sc.hidden_tools,
            guard=get_guard(guard_name), tenant_id=tenant_id, llm=llm,
        )
        async with session:
            session.trace.log("scenario", id=sc.id, category=sc.category, expect=sc.expect.model_dump(),
                              requires_fault_hit=sc.requires_fault_hit, run=n)
            try:
                await drive(session, sc, db)
            except anthropic.APIError:
                pass  # recorded on the turn and in the trace; scored as "error"
            effects = count_effects(db, sc.expect.table)
            s = score(sc, session.summary, effects)
            session.trace.log("score", status=s.status, effects=effects, expected=s.expected,
                              asks=s.asks, fault_hit=s.fault_hit)
    return RunResult(sc, model, guard_name, n, session.summary, s)
