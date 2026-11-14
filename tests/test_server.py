import json

import pytest
from mcp import Client


@pytest.mark.anyio
async def test_charge_then_list(tmp_path, monkeypatch):
    monkeypatch.setenv("DOUBLETAKE_DB", str(tmp_path / "test.db"))
    from kitchen.server import server

    async with Client(server) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        assert set(tools) == {"charge_card", "list_charges"}
        assert tools["list_charges"].annotations.read_only_hint is True

        res = await client.call_tool("charge_card", {"customer": "881", "amount": 50})
        assert not res.is_error
        charge = json.loads(res.content[0].text)
        assert charge["status"] == "succeeded"
        assert charge["charge_id"].startswith("ch_")

        res = await client.call_tool("list_charges", {"customer": "881"})
        charges = json.loads(res.content[0].text)["charges"]
        assert len(charges) == 1
        assert charges[0]["charge_id"] == charge["charge_id"]
        assert charges[0]["amount_cents"] == 5000
