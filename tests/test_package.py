"""The `doubletake` package's public API, with fakes only (no API calls)."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from doubletake import (
    AskUser,
    Block,
    ConversationState,
    DoubleTake,
    Execute,
    HaikuJudge,
    MemoryStore,
    Outcome,
    ReadBack,
    ReturnResult,
    RulesJudge,
)

pytestmark = pytest.mark.anyio
ROOT = Path(__file__).resolve().parents[1]
CHARGE = {"customer": "7", "amount": 5}


class FakeJudge:
    model = "fake"

    def __init__(self, *verdicts):
        self.verdicts, self.prompts = list(verdicts), []

    async def __call__(self, prompt):
        self.prompts.append(prompt)
        return self.verdicts.pop(0), {"input_tokens": 10, "output_tokens": 2}


def new(evidence, count=1, basis="other"):
    return {"verdict": "new", "count": count, "basis": basis, "evidence": evidence}


def guard(judge=None, **tool):
    g = DoubleTake(judge=judge or RulesJudge())
    g.register_tool("charge_card", target_fields=("customer", "amount"), **tool)
    return g


async def test_unregistered_tools_always_execute():
    g = DoubleTake(judge=RulesJudge())
    for _ in range(3):
        assert isinstance(await g.check("c", ["hi"], "search", {"q": "x"}), Execute)
    await g.record("c", "search", {"q": "x"}, Outcome("succeeded"))  # no-op
    assert g.effects("c") == []


async def test_default_judge_is_haiku_and_needs_no_key_until_used():
    assert isinstance(DoubleTake().judge, HaikuJudge)


async def test_reworded_retry_of_a_confirmed_effect_returns_the_original_result():
    g = guard()
    users = ["charge customer 7 $5"]
    assert isinstance(await g.check("c", users, "charge_card", CHARGE), Execute)
    await g.record("c", "charge_card", CHARGE, Outcome("succeeded", '{"id": "ch_1"}'))
    d = await g.check("c", users, "charge_card", {**CHARGE, "note": "retry after timeout"})
    assert isinstance(d, ReturnResult) and d.text.startswith("DoubleTake: done") and "ch_1" in d.text


async def test_unresolved_effect_without_read_back_is_blocked_honestly():
    g = guard()
    users = ["charge customer 7 $5"]
    await g.check("c", users, "charge_card", CHARGE)
    await g.record("c", "charge_card", CHARGE, "timeout")  # string form of the outcome
    d = await g.check("c", users, "charge_card", CHARGE)
    assert isinstance(d, Block) and d.message.startswith("DoubleTake: unknown") and "no way to check it" in d.message
    assert g.effects("c")[0].outcome == "unresolved"


@pytest.mark.parametrize("found,kind", [(True, ReturnResult), (False, Block)])
async def test_read_back_settles_or_blocks(found, kind):
    async def read_back(effect):  # async read-backs are awaited
        assert effect.target_dict == {"customer": "7", "amount": 500}
        return ReadBack("found", {"id": "ch_9"}) if found else ReadBack("absent")

    g = guard(read_back=read_back, read_back_name="the ledger")
    users = ["charge customer 7 $5"]
    await g.check("c", users, "charge_card", CHARGE)
    await g.record("c", "charge_card", CHARGE, Outcome("timeout"))
    d = await g.check("c", users, "charge_card", CHARGE)
    assert isinstance(d, kind)
    assert ("found by the ledger" in d.text) if found else ("does not appear in the ledger yet" in d.message)


async def test_llm_judge_count_allows_two_and_events_are_emitted():
    events = []
    g = DoubleTake(judge=FakeJudge(new("two separate charges", count=2)), on_event=lambda e, **f: events.append(e))
    g.register_tool("charge_card", target_fields=("customer", "amount"))
    users = ["Put through two separate charges of $5 for customer 7."]
    for _ in range(2):
        assert isinstance(await g.check("c", users, "charge_card", CHARGE), Execute)
        await g.record("c", "charge_card", CHARGE, Outcome("succeeded"))
    assert isinstance(await g.check("c", users, "charge_card", CHARGE), ReturnResult)
    assert events.count("judge_call") == 1 and events.count("doubletake") == 3


async def test_unsure_judge_asks_and_rules_judge_treats_new_messages_as_new():
    g = guard(judge=FakeJudge(new("charge customer 7 $5"), {"verdict": "unsure", "count": 0, "basis": "other",
                                                              "evidence": "again"}))
    await g.check("c", ["charge customer 7 $5"], "charge_card", CHARGE)
    await g.record("c", "charge_card", CHARGE, Outcome("succeeded"))
    assert isinstance(await g.check("c", ["charge customer 7 $5", "again"], "charge_card", CHARGE), AskUser)

    r = guard()
    await r.check("c", ["charge customer 7 $5"], "charge_card", CHARGE)
    await r.record("c", "charge_card", CHARGE, Outcome("succeeded"))
    assert isinstance(await r.check("c", ["charge customer 7 $5", "another one"], "charge_card", CHARGE), Execute)


async def test_state_is_per_tenant_and_conversation():
    g = guard()
    for tenant, conv in [("t1", "c"), ("t2", "c"), ("t1", "d")]:
        assert isinstance(await g.check(conv, ["charge 7 $5"], "charge_card", CHARGE, tenant_id=tenant), Execute)
        await g.record(conv, "charge_card", CHARGE, Outcome("succeeded"), tenant_id=tenant)
    assert not isinstance(await g.check("c", ["charge 7 $5"], "charge_card", CHARGE, tenant_id="t1"), Execute)


async def test_custom_store_round_trips_through_json():
    class JsonStore:  # what a Redis/SQL store would do: persist to_dict(), rebuild with from_dict()
        def __init__(self):
            self.rows = {}

        def load(self, tenant_id, conversation_id):
            raw = self.rows.get((tenant_id, conversation_id))
            return ConversationState.from_dict(json.loads(raw)) if raw else None

        def save(self, tenant_id, conversation_id, state):
            self.rows[(tenant_id, conversation_id)] = json.dumps(state.to_dict())

    g = DoubleTake(judge=FakeJudge(new("charge 7 $5")), store=JsonStore())
    g.register_tool("charge_card", target_fields=("customer", "amount"))
    await g.check("c", ["charge 7 $5"], "charge_card", CHARGE, call_id="call_1")
    await g.record("c", "charge_card", CHARGE, Outcome("timeout"), call_id="call_1")
    d = await g.check("c", ["charge 7 $5"], "charge_card", {**CHARGE, "note": "x"})
    assert isinstance(d, Block) and g.effects("c")[0].outcome == "unresolved"


def test_sync_wrappers_and_memory_store():
    g = DoubleTake(judge=RulesJudge(), store=MemoryStore())
    g.register_tool("send_email", target_fields=("to", "subject"))
    args = {"to": "A@x.test", "subject": "Hi", "body": "one"}
    assert isinstance(g.check_sync("c", ["email a@x.test"], "send_email", args), Execute)
    g.record_sync("c", "send_email", args, Outcome("succeeded", "sent"))
    assert isinstance(g.check_sync("c", ["email a@x.test"], "send_email", {**args, "to": "a@x.test", "body": "two"}),
                      ReturnResult)


def test_quickstart_example_runs_offline():
    env = {**os.environ, "PYTHONPATH": str(ROOT), "ANTHROPIC_API_KEY": ""}
    out = subprocess.run([sys.executable, str(ROOT / "examples" / "quickstart.py")], capture_output=True, text=True,
                         env=env, timeout=60)
    assert out.returncode == 0, out.stderr
    assert "did go through (found by the payment ledger)" in out.stdout
    assert out.stdout.strip().endswith("charges in the ledger: 1")


CHARGE_881 = {"customer": "881", "amount": 50}


def decision_text(d) -> str:
    return getattr(d, "text", None) or getattr(d, "message", None) or getattr(d, "question", None) or ""


async def test_tenant_b_does_not_see_tenant_a_confirmed_charge():
    g = DoubleTake(judge=RulesJudge())
    g.register_tool("charge_card", target_fields=("customer", "amount"))
    users = ["charge customer 881 $50"]
    assert isinstance(await g.check("conv-1", users, "charge_card", CHARGE_881, tenant_id="tenant_a"), Execute)
    await g.record("conv-1", "charge_card", CHARGE_881, Outcome("succeeded", '{"charge_id": "ch_A1"}'),
                   tenant_id="tenant_a")
    assert isinstance(await g.check("conv-1", users, "charge_card", CHARGE_881, tenant_id="tenant_a"), ReturnResult)

    decisions = [await g.check("conv-1", users, "charge_card", CHARGE_881, tenant_id="tenant_b")]
    assert isinstance(decisions[0], Execute)
    await g.record("conv-1", "charge_card", CHARGE_881, Outcome("succeeded", '{"charge_id": "ch_B1"}'),
                   tenant_id="tenant_b")
    decisions.append(await g.check("conv-1", users, "charge_card", CHARGE_881, tenant_id="tenant_b"))
    assert all("ch_A1" not in decision_text(d) for d in decisions)
    assert "ch_B1" in decision_text(decisions[1])  # tenant_b's own retry sees only its own charge
    assert [e.result for e in g.effects("conv-1", tenant_id="tenant_b")] == ['{"charge_id": "ch_B1"}']


async def test_tenant_a_unresolved_charge_does_not_block_tenant_b():
    g = DoubleTake(judge=RulesJudge())
    g.register_tool("charge_card", target_fields=("customer", "amount"))
    users = ["charge customer 881 $50"]
    await g.check("conv-1", users, "charge_card", CHARGE_881, tenant_id="tenant_a")
    await g.record("conv-1", "charge_card", CHARGE_881, Outcome("timeout"), tenant_id="tenant_a")
    assert isinstance(await g.check("conv-1", users, "charge_card", CHARGE_881, tenant_id="tenant_a"), Block)

    d = await g.check("conv-1", users, "charge_card", CHARGE_881, tenant_id="tenant_b")
    assert isinstance(d, Execute) and not isinstance(d, (Block, AskUser))
    assert g.effects("conv-1", tenant_id="tenant_a")[0].outcome == "unresolved"
