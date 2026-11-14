"""Server + fault tests. No model, no API key.

Most tests talk to the server in-process; the stdio tests spawn kitchen/server.py
as a subprocess through the agent's own dispatch_tool_call, which also checks that
DOUBLETAKE_DB / DOUBLETAKE_FAULTS reach the server process.
"""

import json
import sqlite3
import time
from types import SimpleNamespace

import pytest
from mcp import Client
from mcp.shared.exceptions import MCPError

import kitchen.server as ks
from kitchen.server import server

pytestmark = pytest.mark.anyio


@pytest.fixture
def env(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    faults = tmp_path / "faults.json"
    faults.write_text("{}")
    monkeypatch.setenv("DOUBLETAKE_DB", str(db))
    monkeypatch.setenv("DOUBLETAKE_FAULTS", str(faults))
    ks.reset_fault_counters()
    yield SimpleNamespace(db=db, faults=faults, set=lambda cfg: faults.write_text(json.dumps(cfg)))
    ks.reset_fault_counters()


def rows(db, table):
    conn = sqlite3.connect(db)
    try:
        return conn.execute(f"SELECT * FROM {table}").fetchall()
    finally:
        conn.close()


def text(res):
    return json.loads(res.content[0].text)


async def test_charge_then_list(env):
    async with Client(server) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        assert set(tools) == {"charge_card", "list_charges", "send_email"}
        assert tools["list_charges"].annotations.read_only_hint is True
        assert tools["send_email"].annotations.read_only_hint is False
        assert tools["send_email"].annotations.idempotent_hint is False

        res = await client.call_tool("charge_card", {"customer": "881", "amount": 50})
        assert not res.is_error
        charge = text(res)
        assert charge["status"] == "succeeded"
        assert charge["charge_id"].startswith("ch_")

        charges = text(await client.call_tool("list_charges", {"customer": "881"}))["charges"]
        assert len(charges) == 1
        assert charges[0]["charge_id"] == charge["charge_id"]
        assert charges[0]["amount_cents"] == 5000


async def test_default_faults_file_is_all_off():
    cfg = json.loads(ks.DEFAULT_FAULTS.read_text())
    assert set(cfg) == {"lose_reply", "pending", "lookup_fails"}
    assert all(not f["enabled"] for f in cfg.values())


async def test_lose_reply_commits_row_and_second_call_is_served_during_hang(env):
    env.set({"lose_reply": {"enabled": True, "tools": ["charge_card"], "hang_seconds": 3, "times": 1}})
    async with Client(server) as client:
        start = time.monotonic()
        with pytest.raises(MCPError) as ei:
            await client.call_tool("charge_card", {"customer": "881", "amount": 50}, read_timeout_seconds=0.5)
        assert "timed out" in str(ei.value).lower()
        assert time.monotonic() - start < 2

        # The charge is committed even though the caller gave up (and the request was cancelled).
        assert len(rows(env.db, "charges")) == 1

        # times: 1 -> the next charge replies normally.
        res = await client.call_tool("charge_card", {"customer": "881", "amount": 50}, read_timeout_seconds=2)
        assert text(res)["status"] == "succeeded"
        assert len(rows(env.db, "charges")) == 2


async def test_second_call_served_while_first_hangs(env):
    """Without the client giving up: one call hangs, another completes meanwhile."""
    import anyio

    env.set({"lose_reply": {"enabled": True, "tools": ["charge_card"], "hang_seconds": 1.5, "times": 1}})
    done = {}
    async with Client(server) as client:

        async def hanging():
            res = await client.call_tool("charge_card", {"customer": "881", "amount": 50})
            done["charge"] = (time.monotonic(), text(res))

        async def quick():
            await anyio.sleep(0.2)  # let the charge start hanging first
            res = await client.call_tool("list_charges", {"customer": "881"})
            done["list"] = (time.monotonic(), text(res))

        async with anyio.create_task_group() as tg:
            tg.start_soon(hanging)
            tg.start_soon(quick)

    # list_charges finished first, and already saw the committed-but-unreplied charge.
    assert done["list"][0] < done["charge"][0] - 0.5
    assert len(done["list"][1]["charges"]) == 1
    assert done["charge"][1]["status"] == "succeeded"


async def test_pending_hides_recent_charges(env):
    async with Client(server) as client:
        await client.call_tool("charge_card", {"customer": "881", "amount": 50})
        env.set({"pending": {"enabled": True, "pending_seconds": 30, "times": 1}})
        assert text(await client.call_tool("list_charges", {"customer": "881"}))["charges"] == []
        # times: 1 -> second lookup is truthful.
        assert len(text(await client.call_tool("list_charges", {"customer": "881"}))["charges"]) == 1


async def test_pending_shows_old_charges(env):
    async with Client(server) as client:
        await client.call_tool("charge_card", {"customer": "881", "amount": 50})
        env.set({"pending": {"enabled": True, "pending_seconds": 0, "times": 5}})
        assert len(text(await client.call_tool("list_charges", {"customer": "881"}))["charges"]) == 1


async def test_lookup_fails_hangs_then_recovers(env):
    env.set({"lookup_fails": {"enabled": True, "hang_seconds": 3, "times": 1}})
    async with Client(server) as client:
        with pytest.raises(MCPError):
            await client.call_tool("list_charges", {"customer": "881"}, read_timeout_seconds=0.5)
        start = time.monotonic()
        res = await client.call_tool("list_charges", {"customer": "881"}, read_timeout_seconds=2)
        assert text(res)["charges"] == []
        assert time.monotonic() - start < 1


async def test_disabled_fault_and_unlisted_tool_do_nothing(env):
    env.set(
        {
            "lose_reply": {"enabled": False, "tools": ["charge_card"], "hang_seconds": 3},
            "lookup_fails": {"enabled": False, "hang_seconds": 3},
        }
    )
    async with Client(server) as client:
        await client.call_tool("charge_card", {"customer": "1", "amount": 1}, read_timeout_seconds=1)
        await client.call_tool("list_charges", {"customer": "1"}, read_timeout_seconds=1)
        # Enabled, but send_email is not in "tools".
        env.set({"lose_reply": {"enabled": True, "tools": ["charge_card"], "hang_seconds": 3}})
        await client.call_tool("send_email", {"to": "a@x", "subject": "s", "body": "b"}, read_timeout_seconds=1)


async def test_times_counts_across_listed_tools(env):
    env.set({"lose_reply": {"enabled": True, "tools": ["charge_card", "send_email"], "hang_seconds": 3, "times": 2}})
    async with Client(server) as client:
        for name, args in [
            ("send_email", {"to": "a@x", "subject": "s", "body": "b"}),
            ("charge_card", {"customer": "1", "amount": 1}),
        ]:
            with pytest.raises(MCPError):
                await client.call_tool(name, args, read_timeout_seconds=0.3)
        res = await client.call_tool("charge_card", {"customer": "1", "amount": 1}, read_timeout_seconds=1)
        assert text(res)["status"] == "succeeded"
    assert len(rows(env.db, "emails")) == 1
    assert len(rows(env.db, "charges")) == 2


async def test_send_email_writes_row(env):
    async with Client(server) as client:
        res = await client.call_tool("send_email", {"to": "c881@example.com", "subject": "Receipt", "body": "Hi"})
        out = text(res)
        assert out["status"] == "sent" and out["email_id"].startswith("em_")
    (row,) = rows(env.db, "emails")
    assert row[0] == out["email_id"]
    assert row[1:4] == ("c881@example.com", "Receipt", "Hi")


async def test_lose_reply_on_send_email_commits_row(env):
    env.set({"lose_reply": {"enabled": True, "tools": ["send_email"], "hang_seconds": 3, "times": 1}})
    async with Client(server) as client:
        with pytest.raises(MCPError):
            await client.call_tool(
                "send_email", {"to": "a@x", "subject": "s", "body": "b"}, read_timeout_seconds=0.3
            )
    assert len(rows(env.db, "emails")) == 1


# ---------------------------------------------------------------- over stdio, via the agent


def tool_use(name, **inp):
    return SimpleNamespace(id="toolu_test_" + name, name=name, input=inp)


async def test_dispatch_timeout_over_stdio(tmp_path, monkeypatch):
    """Real subprocess: env vars reach the server, the agent times out, the row is committed,
    the server keeps serving, and a tool_timeout event is logged."""
    from agent import loop

    monkeypatch.setattr(loop, "LOG_DIR", tmp_path / "logs")
    db, faults = tmp_path / "s.db", tmp_path / "f.json"
    faults.write_text(
        json.dumps({"lose_reply": {"enabled": True, "tools": ["charge_card"], "hang_seconds": 5, "times": 1}})
    )
    trace = loop.TraceLogger("stdio-test")
    try:
        async with Client(loop.server_params(db, faults)) as mcp:
            start = time.monotonic()
            res = await loop.dispatch_tool_call(mcp, tool_use("charge_card", customer="881", amount=50), trace, 1.0)
            assert res["is_error"] is True
            assert res["content"] == loop.TIMEOUT_TEXT
            assert time.monotonic() - start < 3

            res = await loop.dispatch_tool_call(mcp, tool_use("list_charges", customer="881"), trace, 2.0)
            assert not res["is_error"]
            assert len(json.loads(res["content"])["charges"]) == 1
    finally:
        trace.close()

    assert len(rows(db, "charges")) == 1  # landed in OUR db, so DOUBLETAKE_DB was passed
    events = [json.loads(line) for line in trace.path.read_text().splitlines()]
    assert [e["event"] for e in events] == ["tool_call", "tool_timeout", "tool_result", "tool_call", "tool_result"]
    assert events[1]["name"] == "charge_card"


async def test_session_rolls_back_messages_on_api_error(tmp_path, monkeypatch):
    """An API error after a tool round trip must restore history to before the user message."""
    import anthropic

    from agent import loop

    monkeypatch.setattr(loop, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-not-used")

    class FakeAPIError(anthropic.APIError):
        def __init__(self, msg):
            Exception.__init__(self, msg)

    def block(**kw):
        return SimpleNamespace(**kw, to_dict=lambda: kw)

    responses = [
        SimpleNamespace(
            model="fake",
            stop_reason="tool_use",
            usage=SimpleNamespace(input_tokens=10, output_tokens=5, to_dict=lambda: {}),
            content=[block(type="tool_use", id="toolu_1", name="list_charges", input={"customer": "881"})],
        ),
        FakeAPIError("overloaded"),
    ]

    async def fake_create(**kwargs):
        r = responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    async with loop.AgentSession("fake", tmp_path / "s.db", tmp_path / "none.json") as s:
        s.messages.extend([{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}])
        before = list(s.messages)
        monkeypatch.setattr(s._llm.messages, "create", fake_create)
        with pytest.raises(anthropic.APIError):
            await s.send("list charges for 881")
        assert s.messages == before
        assert len(s.summary.tool_calls) == 1  # the tool call still happened and is recorded
