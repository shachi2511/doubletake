"""Phase 3 guard tests. No model, no API key.

Unit tests drive each guard with a fake GuardContext (tests/fakes.py: `step`);
end-to-end tests run a scenario with the FakeLLM against the real stdio server.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from agent.guards import (
    AskUser,
    Block,
    Execute,
    LookupRefused,
    Outcome,
    ReturnResult,
    get_guard,
)
from agent.guards.verify_before_retry import VerifyBeforeRetryGuard
from agent.loop import TIMEOUT_TEXT, AgentSession
from bench.driver import run_scenario
from bench.scenario import load_scenarios
from tests.fakes import FakeLLM, make_ctx, resp, step, text, tool

pytestmark = pytest.mark.anyio
SC = {s.id: s for s in load_scenarios()}
CHARGE = {"customer": "881", "amount": 50.0}
EMAIL = {"to": "alice@example.com", "subject": "March invoice", "body": "Hi"}
TIMEOUT = ("timeout", TIMEOUT_TEXT)


@pytest.fixture
def offline(monkeypatch, tmp_path):
    from agent import loop

    monkeypatch.setattr(loop, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-not-used")


def events(r):
    return [json.loads(line) for line in r.summary.trace_path.read_text().splitlines()]


def charges_reply(*charges):
    return Outcome("succeeded", json.dumps({"customer": "881", "charges": list(charges)}))


def charge_row(cents=5000, at=None):
    at = at or datetime.now(timezone.utc) + timedelta(milliseconds=1)
    return {"charge_id": "ch_found", "customer": "881", "amount_cents": cents, "created_at": at.isoformat(), "note": ""}


# ---------------------------------------------------------------- interface rules


async def test_lookup_refuses_excluded_and_mutating_tools():
    async def fake_lookup(name, args):
        return Outcome("succeeded", "{}")

    ctx = make_ctx(lookup=fake_lookup, excluded=["list_charges"])
    with pytest.raises(LookupRefused, match="not available to the model"):
        await ctx.lookup("list_charges", {"customer": "881"})
    with pytest.raises(LookupRefused, match="not a read-only tool"):
        await make_ctx(lookup=fake_lookup).lookup("charge_card", CHARGE)
    assert (await make_ctx(lookup=fake_lookup).lookup("list_charges", {"customer": "881"})).status == "succeeded"


async def test_lookup_goes_through_the_run_mcp_client_and_is_logged(offline, tmp_path):
    db = tmp_path / "s.db"
    async with AgentSession("fake", db, tmp_path / "none.json", llm=FakeLLM()) as s:
        assert s.guard_ctx.read_only_tools == {"list_charges"}  # from the server's annotations
        out = await s.guard_ctx.lookup("list_charges", {"customer": "881"})
        assert out.status == "succeeded" and json.loads(out.text)["charges"] == []
        with pytest.raises(LookupRefused):
            await s.guard_ctx.lookup("send_email", EMAIL)
    async with AgentSession("fake", db, tmp_path / "none.json", llm=FakeLLM(),
                            exclude_tools=["list_charges"]) as s2:
        with pytest.raises(LookupRefused):  # the server has it, but the model does not
            await s2.guard_ctx.lookup("list_charges", {"customer": "881"})
    ev = [json.loads(line) for line in s.trace.path.read_text().splitlines()]
    looks = [(e["tool"], e["status"]) for e in ev if e["event"] == "guard_lookup"]
    assert looks == [("list_charges", "succeeded"), ("send_email", "refused")]
    assert not any(e["event"] == "tool_call" for e in ev)  # never shown to the model


@pytest.mark.parametrize("name", ["exact_hash", "agent_ledger", "mycelium", "mycelium_turnkey", "verify_before_retry"])
async def test_guard_state_is_keyed_by_tenant_and_run(name):
    """Same guard instance, same calls: only the same (tenant, run) shares state."""
    g = get_guard(name)
    a = make_ctx(run="r1", tenant="t1")
    assert isinstance(await step(g, a, "charge_card", CHARGE), Execute)
    second_same = await step(g, a, "charge_card", CHARGE)
    for other in (make_ctx(run="r2", tenant="t1"), make_ctx(run="r1", tenant="t2")):
        assert isinstance(await step(g, other, "charge_card", CHARGE), Execute)
    if name != "verify_before_retry":  # verify_before_retry does nothing after a success
        assert not isinstance(second_same, Execute)


# ---------------------------------------------------------------- exact_hash


async def test_exact_hash_replays_identical_successful_call():
    g, ctx = get_guard("exact_hash"), make_ctx()
    assert isinstance(await step(g, ctx, "charge_card", CHARGE, ("succeeded", '{"charge_id": "ch_1"}')), Execute)
    d = await step(g, ctx, "charge_card", {"amount": 50, "customer": " 881 "})  # normalized: same key
    assert d == ReturnResult('{"charge_id": "ch_1"}')
    assert isinstance(await step(g, ctx, "charge_card", {**CHARGE, "note": "retry"}), Execute)  # new key


async def test_exact_hash_does_not_cache_timeouts_or_errors_and_skips_reads():
    g, ctx = get_guard("exact_hash"), make_ctx()
    assert isinstance(await step(g, ctx, "charge_card", CHARGE, TIMEOUT), Execute)
    assert isinstance(await step(g, ctx, "charge_card", CHARGE, ("error", "boom")), Execute)
    assert isinstance(await step(g, ctx, "charge_card", CHARGE), Execute)  # still not cached before this
    assert isinstance(await step(g, ctx, "charge_card", CHARGE), ReturnResult)
    for _ in range(2):
        assert isinstance(await step(g, ctx, "list_charges", {"customer": "881"}), Execute)


async def test_exact_hash_end_to_end_stops_forced_repeat(offline):
    llm = FakeLLM(resp(tool("charge_card", "toolu_m", customer="881", amount=50)),
                  resp(text("Charged.")), resp(text("Same charge as before.")))
    r = await run_scenario(SC["forced_repeat_after_success"], "fake", "exact_hash", 1, llm=llm)
    assert [c.decision for c in r.summary.tool_calls] == ["execute", "return_result"]
    assert r.score.effects == 1 and r.score.status == "pass"


# ---------------------------------------------------------------- agent_ledger


async def test_agent_ledger_replays_recorded_success():
    g, ctx = get_guard("agent_ledger"), make_ctx()
    await step(g, ctx, "charge_card", CHARGE, ("succeeded", '{"charge_id": "ch_1"}'))
    assert await step(g, ctx, "charge_card", dict(CHARGE)) == ReturnResult('{"charge_id": "ch_1"}')
    assert isinstance(await step(g, ctx, "charge_card", {**CHARGE, "amount": 51.0}), Execute)


async def test_agent_ledger_blocks_identical_retry_after_timeout_with_its_message():
    g, ctx = get_guard("agent_ledger"), make_ctx()
    await step(g, ctx, "send_email", EMAIL, TIMEOUT)
    d = await step(g, ctx, "send_email", dict(EMAIL))
    assert isinstance(d, Block) and d.message == TIMEOUT_TEXT  # EffectFailedError carries the recorded error


async def test_agent_ledger_does_not_wrap_reads():
    g, ctx = get_guard("agent_ledger"), make_ctx()
    for _ in range(2):
        assert isinstance(await step(g, ctx, "list_charges", {"customer": "881"}), Execute)


# ---------------------------------------------------------------- mycelium


async def test_mycelium_loop_guard_soft_then_hard_on_identical_calls():
    g, ctx = get_guard("mycelium"), make_ctx()
    assert isinstance(await step(g, ctx, "charge_card", CHARGE, TIMEOUT), Execute)
    soft = await step(g, ctx, "charge_card", CHARGE)
    assert isinstance(soft, Block) and "Loop detected" in soft.message
    hard = await step(g, ctx, "charge_card", CHARGE)
    assert isinstance(hard, Block) and "hard-blocked" in hard.message
    # The whole run is frozen now, even for a different call.
    assert isinstance(await step(g, ctx, "list_charges", {"customer": "881"}), Block)


async def test_mycelium_new_tool_call_id_after_a_check_is_a_new_transition():
    """Documented: a new tool_call_id is a new transition; a read in between resets the loop streak."""
    g, ctx = get_guard("mycelium"), make_ctx()
    await step(g, ctx, "charge_card", CHARGE, TIMEOUT)
    assert isinstance(await step(g, ctx, "list_charges", {"customer": "881"}), Execute)
    assert isinstance(await step(g, ctx, "charge_card", CHARGE), Execute)


async def test_mycelium_same_call_id_after_success_returns_completed():
    g, ctx = get_guard("mycelium"), make_ctx()
    await step(g, ctx, "charge_card", CHARGE, ("succeeded", '{"charge_id": "ch_1"}'), tid="toolu_same")
    assert await step(g, ctx, "charge_card", CHARGE, tid="toolu_same") == ReturnResult('{"charge_id": "ch_1"}')


async def test_mycelium_same_call_id_different_args_is_args_drift():
    g, ctx = get_guard("mycelium"), make_ctx()
    await step(g, ctx, "charge_card", CHARGE, tid="toolu_d")
    d = await step(g, ctx, "charge_card", {**CHARGE, "amount": 51.0}, tid="toolu_d")
    assert isinstance(d, Block) and "Identity conflict" in d.message


async def test_mycelium_same_call_id_after_timeout_hard_blocks_unless_reconciled():
    async def not_found(name, args):
        return charges_reply()

    g, ctx = get_guard("mycelium"), make_ctx(lookup=not_found)
    await step(g, ctx, "charge_card", CHARGE, TIMEOUT, tid="toolu_t")
    d = await step(g, ctx, "charge_card", CHARGE, tid="toolu_t")
    assert isinstance(d, AskUser)  # ambiguous mutate: operator release required

    async def found(name, args):
        return charges_reply(charge_row())

    g2, ctx2 = get_guard("mycelium"), make_ctx(lookup=found)
    await step(g2, ctx2, "charge_card", CHARGE, TIMEOUT, tid="toolu_t")
    d2 = await step(g2, ctx2, "charge_card", CHARGE, tid="toolu_t")
    assert isinstance(d2, ReturnResult) and json.loads(d2.text)["charge_id"] == "ch_found"


async def test_mycelium_email_after_timeout_same_id_has_no_reconciler():
    g, ctx = get_guard("mycelium"), make_ctx()
    await step(g, ctx, "send_email", EMAIL, TIMEOUT, tid="toolu_e")
    assert isinstance(await step(g, ctx, "send_email", EMAIL, tid="toolu_e"), AskUser)


# ---------------------------------------------------------------- verify_before_retry


def vbr(wait=0.0):
    g = VerifyBeforeRetryGuard()
    g.wait_s = wait
    return g


async def test_verify_returns_found_charge_after_timeout():
    async def found(name, args):
        return charges_reply(charge_row())

    g, ctx = vbr(), make_ctx(lookup=found)
    await step(g, ctx, "charge_card", CHARGE, TIMEOUT)
    d = await step(g, ctx, "charge_card", {"customer": "881", "amount": 50, "note": "retry"})
    assert isinstance(d, ReturnResult) and json.loads(d.text)["charge_id"] == "ch_found"


async def test_verify_ignores_charges_older_than_the_failed_call():
    async def old_only(name, args):
        return charges_reply(charge_row(at=datetime.now(timezone.utc) - timedelta(hours=1)))

    g, ctx = vbr(), make_ctx(lookup=old_only)
    await step(g, ctx, "charge_card", CHARGE, TIMEOUT)
    assert isinstance(await step(g, ctx, "charge_card", CHARGE), Execute)


async def test_verify_waits_checks_twice_then_allows_one_retry_only():
    looks = []

    async def empty(name, args):
        looks.append(name)
        return charges_reply()

    g, ctx = vbr(), make_ctx(lookup=empty)
    await step(g, ctx, "charge_card", CHARGE, TIMEOUT)
    assert isinstance(await step(g, ctx, "charge_card", CHARGE, TIMEOUT), Execute)
    assert looks == ["list_charges", "list_charges"]  # checked, waited, checked once more
    assert isinstance(await step(g, ctx, "charge_card", CHARGE), Block)  # at most one retry


async def test_verify_asks_when_lookup_fails_or_is_unavailable():
    async def hangs(name, args):
        return Outcome("timeout", TIMEOUT_TEXT)

    for ctx in (make_ctx(lookup=hangs), make_ctx(excluded=["list_charges"])):
        g = vbr()
        await step(g, ctx, "charge_card", CHARGE, TIMEOUT)
        assert isinstance(await step(g, ctx, "charge_card", CHARGE), AskUser)


async def test_verify_asks_for_email_and_ignores_earlier_success():
    g, ctx = vbr(), make_ctx()
    await step(g, ctx, "send_email", EMAIL, TIMEOUT)
    d = await step(g, ctx, "send_email", {**EMAIL, "body": "different body"})
    assert isinstance(d, AskUser) and "no way to check" in d.question
    g2, ctx2 = vbr(), make_ctx()
    await step(g2, ctx2, "charge_card", CHARGE)
    assert isinstance(await step(g2, ctx2, "charge_card", CHARGE), Execute)  # earlier success: nothing special


async def test_verify_end_to_end_truthful_lookup(offline):
    """Real server: the charge commits but times out; the model retries; the guard finds it."""
    llm = FakeLLM(resp(tool("charge_card", "toolu_1", customer="881", amount=50)),
                  resp(tool("charge_card", "toolu_2", customer="881", amount=50)),
                  resp(text("Charged.")), resp(text("Yes.")))
    r = await run_scenario(SC["truthful_lookup"], "fake", "verify_before_retry", 1, llm=llm, tool_timeout=0.5)
    assert [(c.name, c.decision, c.timed_out) for c in r.summary.tool_calls] == [
        ("charge_card", "execute", True), ("charge_card", "return_result", False)]
    assert r.score.effects == 1 and r.score.status == "pass"
    assert [e["status"] for e in events(r) if e["event"] == "guard_lookup"] == ["succeeded"]


# ---------------------------------------------------------------- mycelium_turnkey


def user(ctx, n_turns):
    ctx.messages[:] = [m for i in range(n_turns) for m in ({"role": "user", "content": f"turn {i}"},
                                                           {"role": "assistant", "content": "ok"})]


async def test_turnkey_request_id_is_run_turn_tool():
    from agent.guards.mycelium_guard import MyceliumTurnkeyGuard, user_turn_number

    ctx = make_ctx(run="run-x")
    user(ctx, 2)
    ctx.messages.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": ""}]})
    assert user_turn_number(ctx.messages) == 2  # tool results are not user messages
    from agent.guards import ToolCall as TC
    kw = MyceliumTurnkeyGuard().call_kwargs(ctx, TC("toolu_1", "charge_card", CHARGE, datetime.now(timezone.utc)))
    assert kw["request_id"] == "run-x:user_turn_2:charge_card" and kw["tool_call_id"] == "toolu_1"
    kw = MyceliumTurnkeyGuard().call_kwargs(ctx, TC("toolu_2", "list_charges", {"customer": "881"},
                                                    datetime.now(timezone.utc)))
    assert "request_id" not in kw  # reads keep the derived identity


async def test_turnkey_retry_in_same_turn_reconciles_or_asks():
    async def found(name, args):
        return charges_reply(charge_row())

    g, ctx = get_guard("mycelium_turnkey"), make_ctx(lookup=found)
    user(ctx, 1)
    await step(g, ctx, "charge_card", CHARGE, TIMEOUT)
    assert isinstance(await step(g, ctx, "list_charges", {"customer": "881"}), Execute)
    d = await step(g, ctx, "charge_card", CHARGE)  # new tool_use_id, same host request_id
    assert isinstance(d, ReturnResult) and json.loads(d.text)["charge_id"] == "ch_found"

    async def empty(name, args):
        return charges_reply()

    g2, ctx2 = get_guard("mycelium_turnkey"), make_ctx(lookup=empty)
    user(ctx2, 1)
    await step(g2, ctx2, "charge_card", CHARGE, TIMEOUT)
    await step(g2, ctx2, "list_charges", {"customer": "881"})
    assert isinstance(await step(g2, ctx2, "charge_card", CHARGE), AskUser)  # unreconciled ambiguity


async def test_turnkey_reworded_retry_or_second_item_in_same_turn_is_identity_conflict():
    g, ctx = get_guard("mycelium_turnkey"), make_ctx()
    user(ctx, 1)
    await step(g, ctx, "charge_card", {"customer": "881", "amount": 5.0})
    d = await step(g, ctx, "charge_card", {"customer": "881", "amount": 5.0, "note": "second coffee"})
    assert isinstance(d, Block) and "Identity conflict" in d.message
    user(ctx, 2)  # a new user message gets a new key
    assert isinstance(await step(g, ctx, "charge_card", {"customer": "881", "amount": 5.0, "note": "x"}), Execute)
