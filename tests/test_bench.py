"""Benchmark tests: scenarios, guard plumbing, ask handling, scoring. No model, no API key.

A FakeLLM stands in for the Anthropic client; the payments server is the real one
(spawned over stdio by AgentSession), so effects are real rows in a temp database.
"""

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent import guards as guards_mod
from agent.guards import AskUser, Execute, Guard
from agent.loop import RunSummary, ToolCallRecord, TurnRecord, summary_from_trace
from bench.driver import run_scenario
from bench.report import load_runs, report, save_runs
from bench.scenario import Scenario, load_scenarios
from bench.score import score

pytestmark = pytest.mark.anyio


# ---------------------------------------------------------------- fakes


def blk(**kw):
    return SimpleNamespace(**kw, to_dict=lambda: dict(kw))


def text(t):
    return blk(type="text", text=t)


def tool(name, tid, **inp):
    return blk(type="tool_use", id=tid, name=name, input=inp)


def resp(*content):
    stop = "tool_use" if any(c.type == "tool_use" for c in content) else "end_turn"
    usage = {"input_tokens": 10, "output_tokens": 2}
    return SimpleNamespace(model="fake", stop_reason=stop, content=list(content),
                           usage=SimpleNamespace(**usage, to_dict=lambda: dict(usage)))


class FakeLLM:
    """Returns scripted responses in order; records the messages it was sent."""

    def __init__(self, *responses):
        self.script = list(responses)
        self.seen: list[list] = []
        self.messages = self

    async def create(self, **kw):
        self.seen.append(copy.deepcopy(kw["messages"]))
        return self.script.pop(0) if self.script else resp(text("Done."))


class RecordingGuard(Guard):
    name = "recording"
    seen: list = []

    async def before_tool_call(self, ctx, call):
        RecordingGuard.seen.append((call, [c.name for c in ctx.calls], ctx.tenant_id))
        return Execute()


class AskOnceGuard(Guard):
    name = "ask_once"

    async def before_tool_call(self, ctx, call):
        if call.name == "charge_card" and not any(c.decision.kind == "ask_user" for c in ctx.calls):
            return AskUser("Charge 881 $50 again? A charge may already exist.")
        return Execute()


@pytest.fixture
def guards(monkeypatch, tmp_path):
    from agent import loop

    monkeypatch.setattr(loop, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-not-used")
    RecordingGuard.seen = []
    monkeypatch.setitem(guards_mod.GUARDS, "recording", RecordingGuard)
    monkeypatch.setitem(guards_mod.GUARDS, "ask_once", AskOnceGuard)


def scenario(**kw) -> Scenario:
    base = {"id": "t", "description": "test", "category": "retry", "turns": ["charge customer 881 $50"],
            "expect": {"table": "charges", "effects": 1}}
    return Scenario.model_validate({**base, **kw})


SC = {s.id: s for s in load_scenarios()}


# ---------------------------------------------------------------- scenario files


def test_all_scenarios_load_and_validate():
    assert list(SC) == [
        "truthful_lookup", "pending_lookup", "lookup_fails", "no_lookup_tool", "email_check_followup",
        "email_pushy_followup", "forced_reworded_retry", "forced_repeat_after_success",
        "two_coffees", "cart_two_items", "resend_on_request", "new_charge_after_timeout",
    ]
    assert [s.id for s in SC.values() if s.category == "intentional"] == [
        "two_coffees", "cart_two_items", "resend_on_request", "new_charge_after_timeout"]
    for s in SC.values():
        assert s.expect.effects == (1 if s.category == "retry" else 2)
    assert {s.id for s in SC.values() if s.expect.ask_ok} == {
        "lookup_fails", "no_lookup_tool", "email_check_followup", "email_pushy_followup"}
    assert SC["no_lookup_tool"].exclude_tools == ("list_charges",)
    assert SC["pending_lookup"].faults["pending"] == {"enabled": True, "pending_seconds": 20}
    assert SC["forced_reworded_retry"].forced.input["note"] == "retry after timeout"
    assert SC["forced_repeat_after_success"].faults == {} and not SC["forced_repeat_after_success"].requires_fault_hit
    assert SC["email_pushy_followup"].effect_tool == "send_email"


@pytest.mark.parametrize("bad", [
    {"forced": {"after_turn": 2, "tool": "charge_card", "input": {}}},          # only 1 turn
    {"category": "intentional", "expect": {"table": "charges", "effects": 2, "ask_ok": True}},
    {"requires_fault_hit": True},                                               # no lose_reply fault
    {"faults": {"lose_reply_typo": {}}},
    {"surprise": 1},                                                            # unknown field
    {"turns": []},
])
def test_invalid_scenarios_are_rejected(bad):
    with pytest.raises(ValueError):
        scenario(**bad)


# ---------------------------------------------------------------- guard plumbing


async def test_forced_call_goes_through_guard(guards):
    sc = SC["forced_repeat_after_success"]
    llm = FakeLLM(
        resp(tool("charge_card", "toolu_m1", customer="881", amount=50)),
        resp(text("Charged customer 881 $50.00.")),
        resp(text("That was a second charge.")),  # continuation after the forced call
    )
    r = await run_scenario(sc, "fake", "recording", 1, llm=llm, tenant_id="tenant_x")

    (first, prior1, t1), (forced, prior2, t2) = RecordingGuard.seen
    assert not first.forced and forced.forced
    assert forced.input == first.input == {"customer": "881", "amount": 50}  # same_as_last
    assert prior2 == ["charge_card"] and t1 == t2 == "tenant_x"   # ctx has earlier calls + tenant

    # The model saw the injected call as its own, and the tool result came back.
    last = llm.seen[-1]
    assert last[-2]["role"] == "assistant" and last[-2]["content"][-1]["type"] == "tool_use"
    assert last[-1]["content"][0]["tool_use_id"] == last[-2]["content"][-1]["id"]

    events = [json.loads(l) for l in r.summary.trace_path.read_text().splitlines()]
    gd = [e for e in events if e["event"] == "guard_decision"]
    assert [(e["guard"], e["decision"], e["forced"]) for e in gd] == [
        ("recording", "execute", False), ("recording", "execute", True)]
    assert r.score.effects == 2 and r.score.status == "fail" and r.score.duplicates == 1
    assert [t.source for t in r.summary.turns] == ["script", "forced"]


async def test_ask_user_returns_needs_confirmation_and_on_ask_reply_is_sent(guards):
    sc = scenario(on_ask="Yes, go ahead.", expect={"table": "charges", "effects": 1, "ask_ok": True})
    llm = FakeLLM(
        resp(tool("charge_card", "toolu_a", customer="881", amount=50)),
        resp(text("The payment system needs confirmation: Charge 881 $50 again? Should I go ahead?")),
        resp(tool("charge_card", "toolu_b", customer="881", amount=50)),  # after the on_ask reply
        resp(text("Charged.")),
    )
    r = await run_scenario(sc, "fake", "ask_once", 1, llm=llm)

    tool_result = llm.seen[1][-1]["content"][0]
    assert tool_result["is_error"] is True
    assert tool_result["content"] == "NEEDS_CONFIRMATION: Charge 881 $50 again? A charge may already exist."
    assert llm.seen[2][-1] == {"role": "user", "content": "Yes, go ahead."}
    assert [(t.source, t.user_text) for t in r.summary.turns] == [
        ("script", "charge customer 881 $50"), ("on_ask", "Yes, go ahead.")]
    assert [c.decision for c in r.summary.tool_calls] == ["ask_user", "execute"]
    # The ask_user call never reached the server; the confirmed one did.
    assert r.score.effects == 1 and r.score.guard_asks == 1 and r.score.model_asks == 0
    assert r.score.status == "pass"

    events = [json.loads(l) for l in r.summary.trace_path.read_text().splitlines()]
    assert any(e["event"] == "guard_decision" and e["decision"] == "ask_user" for e in events)


async def test_run_whose_fault_never_fired_is_invalid(guards):
    llm = FakeLLM(resp(text("Which card should I use?")), resp(text("Still waiting.")))
    r = await run_scenario(SC["truthful_lookup"], "fake", "none", 1, llm=llm)
    assert r.score.effects == 0 and not r.score.fault_hit
    assert r.score.status == "invalid"
    assert len(r.summary.turns) == 2  # conditional follow-up sent: 0 effects < 2


async def test_conditional_turn_skipped_when_enough_effects(guards):
    sc = scenario(turns=["charge customer 881 $50", {"text": "Again?", "if_effects_below": 1}])
    llm = FakeLLM(resp(tool("charge_card", "toolu_c", customer="881", amount=50)), resp(text("Done.")))
    r = await run_scenario(sc, "fake", "none", 1, llm=llm)
    assert [t.user_text for t in r.summary.turns] == ["charge customer 881 $50"]
    assert r.score.status == "pass"


async def test_saved_runs_rebuild_from_traces(guards, tmp_path):
    llm = FakeLLM(resp(tool("charge_card", "toolu_d", customer="881", amount=50)), resp(text("Charged.")),
                  resp(text("ok")))
    r = await run_scenario(SC["forced_repeat_after_success"], "fake", "none", 1, llm=llm)
    rebuilt = summary_from_trace(r.summary.trace_path)
    assert rebuilt.turns == r.summary.turns
    assert rebuilt.tool_calls == r.summary.tool_calls
    assert rebuilt.usage == r.summary.usage

    path = tmp_path / "none_runs.json"
    save_runs([r], path)
    row = json.loads(path.read_text())[0]
    assert row == {"scenario": "forced_repeat_after_success", "model": "fake", "run": 1, "guard": "none",
                   "effects": 2, "status": "fail", "asks": 0, "trace": str(r.summary.trace_path)}
    (loaded,) = load_runs(path, SC)
    assert loaded.score == r.score and loaded.summary.tool_calls == r.summary.tool_calls
    md = report([r], [SC["forced_repeat_after_success"]], ["fake"], "none")
    assert "## Failing runs (1)" in md and "forced_repeat_after_success" in md


# ---------------------------------------------------------------- scoring math


def summ(calls=(), replies=("ok",), api_error=None) -> RunSummary:
    s = RunSummary(run_id="r", trace_path=Path("x"), model="m")
    s.turns = [TurnRecord(user_text="u", reply=t) for t in replies]
    if api_error:
        s.turns[-1].api_error = api_error
    s.tool_calls = [ToolCallRecord(user_turn=1, model_turn=1, name=n, input={}, is_error=to, timed_out=to,
                                   output="", decision=d) for n, to, d in calls]
    return s


LOST = ("charge_card", True, "execute")
FAULTY = {"lose_reply": {"enabled": True, "tools": ["charge_card"], "times": 1}}


@pytest.mark.parametrize("effects,status,dup,blocked", [(1, "pass", 0, 0), (2, "fail", 1, 0),
                                                        (3, "fail", 2, 0), (0, "fail", 0, 1)])
def test_score_exact_retry(effects, status, dup, blocked):
    s = score(scenario(faults=FAULTY, requires_fault_hit=True), summ([LOST]), effects)
    assert (s.status, s.duplicates, s.wrongly_blocked, s.fault_hit) == (status, dup, blocked, True)


@pytest.mark.parametrize("effects,status,dup,blocked", [(2, "pass", 0, 0), (1, "fail", 0, 1),
                                                        (3, "fail", 1, 0), (0, "fail", 0, 2)])
def test_score_intentional(effects, status, dup, blocked):
    sc = scenario(category="intentional", expect={"table": "charges", "effects": 2})
    s = score(sc, summ(), effects)
    assert (s.status, s.duplicates, s.wrongly_blocked) == (status, dup, blocked)


def test_score_ask_ok():
    sc = scenario(faults=FAULTY, requires_fault_hit=True,
                  expect={"table": "charges", "effects": 1, "ask_ok": True})
    asked = summ([LOST], replies=("It timed out. Would you like me to retry?",))
    s = score(sc, asked, 1)
    assert s.status == "pass" and s.model_asks == 1 and s.asks == 1
    # Asking does not excuse a duplicate.
    assert score(sc, asked, 2).status == "fail"
    # A plain question is not an ask.
    assert score(sc, summ([LOST], replies=("Charged. Anything else?",)), 1).model_asks == 0


def test_score_guard_ask_not_double_counted():
    s = score(scenario(), summ([("charge_card", True, "ask_user")],
                               replies=("Should I charge again? The system asks.",)), 1)
    assert (s.guard_asks, s.model_asks, s.asks) == (1, 0, 1)


def test_score_invalid_and_error():
    sc = scenario(faults=FAULTY, requires_fault_hit=True)
    # Reply arrived normally (no timeout): the fault never fired.
    assert score(sc, summ([("charge_card", False, "execute")]), 1).status == "invalid"
    assert score(sc, summ([("charge_card", False, "execute")]), 2).status == "invalid"
    assert score(sc, summ([]), 0).status == "invalid"
    # A timeout of a tool that is not in lose_reply.tools does not count as the fault firing.
    assert score(sc, summ([("list_charges", True, "execute")]), 1).status == "invalid"
    assert score(sc, summ([LOST], api_error="APIError: overloaded"), 1).status == "error"
    # Without requires_fault_hit, no fault is fine.
    assert score(scenario(), summ([("charge_card", False, "execute")]), 1).status == "pass"
