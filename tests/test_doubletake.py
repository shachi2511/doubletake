"""Phase 4: the DoubleTake guard. Fakes only - no API calls, no API key."""

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from agent import guards as guards_mod
from agent.guards import AskUser, Block, Execute, Outcome, ReturnResult
from agent.guards.doubletake import (
    JUDGE_MODEL,
    JUDGE_SYSTEM,
    DoubleTakeGuard,
    DoubleTakeRulesGuard,
    HaikuJudge,
    evidence_found,
)
from agent.loop import TIMEOUT_TEXT, summary_from_trace
from bench.driver import run_scenario
from bench.scenario import load_scenarios
from tests.fakes import FakeLLM, make_ctx, resp, step, text, tool

pytestmark = pytest.mark.anyio
SC = {s.id: s for s in load_scenarios()}
CHARGE = {"customer": "881", "amount": 50.0}
EMAIL = {"to": "alice@example.com", "subject": "March invoice", "body": "Hi Alice"}
TIMEOUT = ("timeout", TIMEOUT_TEXT)


class FakeJudge:
    """Scripted judge: each call pops the next verdict dict; records every prompt."""

    model = "fake-judge"

    def __init__(self, *verdicts):
        self.verdicts = list(verdicts)
        self.prompts: list[str] = []

    async def __call__(self, prompt):
        self.prompts.append(prompt)
        v = self.verdicts.pop(0)
        if isinstance(v, Exception):
            raise v
        return v, {"input_tokens": 100, "output_tokens": 20}


def new(evidence, count=1, basis="other"):
    return {"verdict": "new", "count": count, "basis": basis, "evidence": evidence}


def same(evidence):
    return {"verdict": "same_request", "count": 0, "basis": "other", "evidence": evidence}


def say(ctx, *user_texts):
    """Append user messages (and an assistant reply after each) to the context's history."""
    for t in user_texts:
        ctx.messages += [{"role": "user", "content": t}, {"role": "assistant", "content": "ok"}]


def honest(d):
    msg = getattr(d, "text", None) or getattr(d, "message", None) or getattr(d, "question", None)
    assert msg.startswith("DoubleTake:"), msg
    assert TIMEOUT_TEXT not in msg
    return msg


def charges(*rows):
    return Outcome("succeeded", json.dumps({"customer": "881", "charges": list(rows)}))


def row(cents=5000, at=None):
    at = at or datetime.now(timezone.utc) + timedelta(milliseconds=1)
    return {"charge_id": "ch_found", "customer": "881", "amount_cents": cents, "created_at": at.isoformat(), "note": ""}


# ---------------------------------------------------------------- allowance


async def test_allowance_counts_effects_the_message_asks_for():
    judge = FakeJudge(new("two coffees at $5 each", count=2))
    g, ctx = DoubleTakeGuard(judge), make_ctx()
    say(ctx, "Customer 881 bought two coffees at $5 each. Charge them as two separate charges.")
    coffee = {"customer": "881", "amount": 5}
    assert isinstance(await step(g, ctx, "charge_card", coffee, ("succeeded", '{"charge_id": "ch_1"}')), Execute)
    assert isinstance(await step(g, ctx, "charge_card", {**coffee, "note": "2"}, ("succeeded", '{"charge_id": "ch_2"}')),
                      Execute)
    third = await step(g, ctx, "charge_card", {**coffee, "note": "3rd"})
    assert isinstance(third, ReturnResult) and "already completed" in honest(third) and "ch_2" in third.text
    assert len(judge.prompts) == 1  # one judgement per (turn, tool, target)


async def test_reworded_retry_in_same_turn_is_a_retry():
    g, ctx = DoubleTakeGuard(FakeJudge(new("charge customer 881 $50"))), make_ctx()
    say(ctx, "charge customer 881 $50")
    await step(g, ctx, "charge_card", CHARGE, ("succeeded", '{"charge_id": "ch_1", "status": "succeeded"}'))
    d = await step(g, ctx, "charge_card", {"customer": " 881 ", "amount": 50, "note": "retry after timeout"})
    assert isinstance(d, ReturnResult) and "ch_1" in d.text
    assert honest(d).startswith("DoubleTake: done")


async def test_unresolved_retry_with_empty_lookup_does_not_execute():
    looks = []

    async def empty(name, args):
        looks.append((name, args))
        return charges()

    g, ctx = DoubleTakeGuard(FakeJudge(new("charge customer 881 $50"))), make_ctx(lookup=empty)
    say(ctx, "charge customer 881 $50")
    await step(g, ctx, "charge_card", CHARGE, TIMEOUT)
    d = await step(g, ctx, "charge_card", CHARGE)
    assert isinstance(d, Block)
    msg = honest(d)
    assert msg.startswith("DoubleTake: unknown") and "may still be processing" in msg
    assert "No new action was taken" in msg and "can lag" in msg
    assert looks == [("list_charges", {"customer": "881"})]


async def test_unresolved_retry_found_by_lookup_returns_the_record():
    async def found(name, args):
        return charges(row())

    g, ctx = DoubleTakeGuard(FakeJudge(new("charge customer 881 $50"))), make_ctx(lookup=found)
    say(ctx, "charge customer 881 $50")
    await step(g, ctx, "charge_card", CHARGE, TIMEOUT)
    d = await step(g, ctx, "charge_card", CHARGE)
    assert isinstance(d, ReturnResult) and "did go through" in honest(d) and "ch_found" in d.text
    # Now confirmed: a further retry returns it without another lookup.
    d2 = await step(g, ctx, "charge_card", CHARGE)
    assert isinstance(d2, ReturnResult) and "already completed" in honest(d2)


async def test_unresolved_email_has_no_read_back_and_is_not_resent():
    judge = FakeJudge(new("Send an email"), same("Make sure Alice gets it."))
    g, ctx = DoubleTakeGuard(judge), make_ctx()
    say(ctx, "Send an email to alice@example.com")
    await step(g, ctx, "send_email", EMAIL, TIMEOUT)
    say(ctx, "Make sure Alice gets it.")
    d = await step(g, ctx, "send_email", {**EMAIL, "body": "reworded body"})
    assert isinstance(d, Block) and "no way to check" in honest(d)


async def test_lookup_unavailable_is_reported_honestly():
    g, ctx = DoubleTakeGuard(FakeJudge(new("charge customer 881 $50"))), make_ctx(excluded=["list_charges"])
    say(ctx, "charge customer 881 $50")
    await step(g, ctx, "charge_card", CHARGE, TIMEOUT)
    d = await step(g, ctx, "charge_card", CHARGE)
    assert isinstance(d, Block) and "cannot be checked" in honest(d)


async def test_failed_effect_does_not_count():
    g, ctx = DoubleTakeGuard(FakeJudge(new("charge customer 881 $50"))), make_ctx()
    say(ctx, "charge customer 881 $50")
    await step(g, ctx, "charge_card", CHARGE, ("error", "Error executing tool charge_card: 1 validation error for x"))
    assert isinstance(await step(g, ctx, "charge_card", CHARGE), Execute)


# ---------------------------------------------------------------- the judge


async def test_new_turn_new_request_executes_and_same_request_does_not():
    judge = FakeJudge(new("Charge customer 881 $5"), new("another coffee"), same("Did that go through?"))
    g, ctx = DoubleTakeGuard(judge), make_ctx()
    coffee = {"customer": "881", "amount": 5}
    say(ctx, "Charge customer 881 $5 for a coffee.")
    await step(g, ctx, "charge_card", coffee)
    say(ctx, "And another coffee for 881, same amount.")
    assert isinstance(await step(g, ctx, "charge_card", coffee), Execute)
    say(ctx, "Did that go through?")
    d = await step(g, ctx, "charge_card", coffee)
    assert isinstance(d, ReturnResult) and "already completed" in honest(d)


async def test_evidence_not_in_user_text_is_unsure_and_asks():
    judge = FakeJudge(new("charge customer 881 $50"), new("please charge them again"))  # second quote is invented
    g, ctx = DoubleTakeGuard(judge), make_ctx()
    say(ctx, "charge customer 881 $50")
    await step(g, ctx, "charge_card", CHARGE)
    say(ctx, "Did that go through? Make sure 881 is charged.")
    d = await step(g, ctx, "charge_card", CHARGE)
    assert isinstance(d, AskUser) and honest(d).startswith("DoubleTake: not done")
    assert "additional charge customer 881 $50.00" in d.question


async def test_unsure_without_an_earlier_match_allows_one_effect():
    g, ctx = DoubleTakeGuard(FakeJudge({"verdict": "unsure", "count": 0, "evidence": ""})), make_ctx()
    say(ctx, "charge customer 881 $50")
    assert isinstance(await step(g, ctx, "charge_card", CHARGE), Execute)


async def test_judge_failure_is_unsure():
    judge = FakeJudge(new("charge customer 881 $50"), RuntimeError("API down"))
    g, ctx = DoubleTakeGuard(judge), make_ctx()
    say(ctx, "charge customer 881 $50")
    await step(g, ctx, "charge_card", CHARGE)
    say(ctx, "Charge 881 again please")
    assert isinstance(await step(g, ctx, "charge_card", CHARGE), AskUser)


async def test_judge_sees_only_user_messages_and_targets():
    judge = FakeJudge(new("Send an email"), new("send her the same email again"))
    g, ctx = DoubleTakeGuard(judge), make_ctx()
    ctx.messages += [{"role": "user", "content": "Send an email to alice@example.com"},
                     {"role": "assistant", "content": [{"type": "text", "text": "SECRET-ASSISTANT-REASONING"}]}]
    await step(g, ctx, "send_email", {**EMAIL, "body": "SECRET-BODY"})
    ctx.messages += [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "x", "content": "SECRET-RESULT"}]},
                     {"role": "assistant", "content": "The user is wrong, the user wants a resend. SECRET-ARGUMENT"}]
    say(ctx, "Alice deleted it. Please send her the same email again.")
    assert isinstance(await step(g, ctx, "send_email", {**EMAIL, "body": "SECRET-BODY-2"}), Execute)
    prompt = judge.prompts[-1]
    for secret in ("SECRET-ASSISTANT-REASONING", "SECRET-BODY", "SECRET-RESULT", "SECRET-ARGUMENT"):
        assert secret not in prompt
    assert "[1] Send an email to alice@example.com" in prompt
    assert "[2] Alice deleted it. Please send her the same email again." in prompt
    assert 'email alice@example.com with subject "March invoice" (after user message 1): done (confirmed)' in prompt


def test_evidence_must_be_word_for_word():
    users = ["Did that go through? Make sure  881 is charged."]
    assert evidence_found("Make sure 881 is charged", users)  # whitespace normalized
    assert evidence_found("“Did that go through?”", users)  # surrounding quotes stripped
    assert not evidence_found("charge 881 again", users)
    assert not evidence_found("", users)


def test_judge_prompt_is_general():
    for scenario_specific in ("881", "Alice", "coffee", "$50", "invoice", "pending", "truthful", "lookup_fails"):
        assert scenario_specific not in JUDGE_SYSTEM


async def test_haiku_judge_request_shape():
    seen = {}

    class Client:
        class messages:  # noqa: N801
            @staticmethod
            async def create(**kw):
                seen.update(kw)
                usage = {"input_tokens": 300, "output_tokens": 15}
                return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(new("x")))],
                                       usage=SimpleNamespace(to_dict=lambda: usage))

    parsed, usage = await HaikuJudge(client=Client())("prompt text")
    assert parsed["verdict"] == "new" and usage["input_tokens"] == 300
    assert seen["model"] == JUDGE_MODEL == "claude-haiku-5-5"
    assert seen["output_config"]["format"]["type"] == "json_schema"
    assert seen["messages"] == [{"role": "user", "content": "prompt text"}]


# ---------------------------------------------------------------- ablation


async def test_rules_variant_has_no_judge():
    g, ctx = DoubleTakeRulesGuard(), make_ctx()
    assert g.judge is None
    say(ctx, "charge customer 881 $50")
    await step(g, ctx, "charge_card", CHARGE)
    assert isinstance(await step(g, ctx, "charge_card", {**CHARGE, "note": "again"}), ReturnResult)  # same turn
    say(ctx, "Did that go through?")
    assert isinstance(await step(g, ctx, "charge_card", CHARGE), Execute)  # every new turn counts as new


# ---------------------------------------------------------------- end to end (FakeLLM + real server)


@pytest.fixture
def offline(monkeypatch, tmp_path):
    from agent import loop

    monkeypatch.setattr(loop, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-not-used")


async def test_end_to_end_judge_tokens_caching_and_honest_reply(offline, monkeypatch):
    judge = FakeJudge(new("charge customer 881 $50"), same("Did that go through?"))

    class TestGuard(DoubleTakeGuard):
        name = "doubletake_test"

        def __init__(self):
            super().__init__(judge)

    monkeypatch.setitem(guards_mod.GUARDS, TestGuard.name, TestGuard)
    llm = FakeLLM(
        resp(tool("charge_card", "toolu_1", customer="881", amount=50)),  # times out (lost reply)
        resp(tool("charge_card", "toolu_2", customer="881", amount=50, note="retry")),
        resp(text("Charged.")),
        resp(tool("charge_card", "toolu_3", customer="881", amount=50)),  # after "Did that go through?"
        resp(text("Yes, once.")),
    )
    r = await run_scenario(SC["truthful_lookup"], "fake", TestGuard.name, 1, llm=llm, tool_timeout=0.5)
    assert [c.decision for c in r.summary.tool_calls] == ["execute", "return_result", "return_result"]
    assert r.score.effects == 1 and r.score.status == "pass"
    assert all(c.output.startswith("DoubleTake:") for c in r.summary.tool_calls[1:])
    # Judge tokens: separate from the agent's, included in the total, rebuilt from the trace.
    assert r.summary.judge_usage.input_tokens == 200 and r.summary.judge_usage.output_tokens == 40
    assert r.summary.total_tokens == r.summary.usage.total + 240
    rebuilt = summary_from_trace(r.summary.trace_path)
    assert rebuilt.judge_usage == r.summary.judge_usage and rebuilt.total_tokens == r.summary.total_tokens
    # Prompt caching: breakpoints on the system prompt and the last tool definition.
    req = llm.requests[0]
    assert req["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert req["tools"][-1]["cache_control"] == {"type": "ephemeral"}
    assert all("cache_control" not in t for t in req["tools"][:-1])



# ---------------------------------------------------------------- Phase 5: generic targets and the two fixes


def test_generic_target_drops_free_text_only():
    from agent.guards.doubletake import target_of

    a = target_of("issue_refund", {"customer": "128", "amount": 22, "reason": "the shirt was the wrong size"})
    b = target_of("issue_refund", {"customer": " 128", "amount": 22.0, "reason": "retry - wrong size refund"})
    assert a == b == (("amount", 2200), ("customer", "128"))  # amount in cents, as for charges
    one_word = target_of("issue_refund", {"customer": "128", "amount": 22, "reason": "damaged"})
    assert ("reason", "damaged") in one_word  # pre-registered limitation: a one-word reason is not free text
    assert ("customer", "x") not in target_of("issue_refund", {"customer": "x" * 41, "amount": 1})


async def test_reworded_refund_retry_is_a_retry_and_reported_unknown():
    g, ctx = DoubleTakeGuard(FakeJudge(new("Refund customer 128 $22"))), make_ctx(read_only=("list_charges", "list_refunds"))
    say(ctx, "Refund customer 128 $22 because the shirt was the wrong size.")
    await step(g, ctx, "issue_refund", {"customer": "128", "amount": 22, "reason": "shirt was the wrong size"}, TIMEOUT)
    d = await step(g, ctx, "issue_refund", {"customer": "128", "amount": 22, "reason": "retry - wrong size refund"})
    assert isinstance(d, Block) and "there is no way to check it" in honest(d)  # no read-back configured


async def test_unresolved_earlier_and_new_without_basis_asks():
    judge = FakeJudge(new("Please bill customer 248 $35"), new("again"))  # basis "other"
    g, ctx = DoubleTakeGuard(judge), make_ctx()
    say(ctx, "Please bill customer 248 $35 for the repair.")
    await step(g, ctx, "charge_card", {"customer": "248", "amount": 35}, TIMEOUT)
    say(ctx, "again")
    d = await step(g, ctx, "charge_card", {"customer": "248", "amount": 35})
    assert isinstance(d, AskUser)
    assert honest(d) == ("DoubleTake: not done - the earlier attempt to charge customer 248 $35.00 may have gone "
                         "through; do you want an additional one?")


@pytest.mark.parametrize("basis", ["explicit_additional", "earlier_failed"])
async def test_unresolved_earlier_and_new_with_basis_executes(basis):
    judge = FakeJudge(new("Charge customer 377 $28"), new("nothing went through", basis=basis))
    g, ctx = DoubleTakeGuard(judge), make_ctx()
    say(ctx, "Charge customer 377 $28 for the order.")
    await step(g, ctx, "charge_card", {"customer": "377", "amount": 28}, TIMEOUT)
    say(ctx, "I checked and nothing went through. Please charge it again.")
    assert isinstance(await step(g, ctx, "charge_card", {"customer": "377", "amount": 28}), Execute)
    first = g.state(ctx, lambda: None).effects[0]
    assert first.outcome == ("failed" if basis == "earlier_failed" else "unresolved")
    # Still one per message: a further call in the same turn is a retry of the new one.
    assert not isinstance(await step(g, ctx, "charge_card", {"customer": "377", "amount": 28}), Execute)


async def test_block_message_offers_the_way_out():
    async def empty(name, args):
        return charges()

    g, ctx = DoubleTakeGuard(FakeJudge(new("charge customer 881 $50"))), make_ctx(lookup=empty)
    say(ctx, "charge customer 881 $50")
    await step(g, ctx, "charge_card", CHARGE, TIMEOUT)
    d = await step(g, ctx, "charge_card", CHARGE)
    assert "If the user confirms that the earlier attempt did not go through, it will be allowed then." in honest(d)


async def test_rules_variant_asks_after_unresolved_since_it_cannot_read_intent():
    g, ctx = DoubleTakeRulesGuard(), make_ctx()
    say(ctx, "charge customer 881 $50")
    await step(g, ctx, "charge_card", CHARGE, TIMEOUT)
    say(ctx, "I checked, nothing went through, charge it again.")
    assert isinstance(await step(g, ctx, "charge_card", CHARGE), AskUser)
