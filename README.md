# DoubleTake

Research project on duplicate side effects in AI agents.

## Phase 0: baseline agent + fake payments server

A Claude agent (`agent/loop.py`) calls a fake payments MCP server (`kitchen/server.py`) over stdio.
The server stores charges in SQLite at `data/payments.db`.

```
kitchen/server.py       fake payments MCP server (MCP Python SDK 2.x, MCPServer)
agent/loop.py           agent loop + REPL; tool calls go through dispatch_tool_call()
scripts/show_charges.py prints the charges and emails tables (--charges / --emails)
scripts/demo_double_charge.py  Phase 1 lost-reply demo -> results/phase1_demo.md
kitchen/faults.json     fault switches (all off)
data/                   SQLite db (git-ignored)
logs/                   JSONL traces, one file per run (git-ignored)
tests/                  pytest, connects to the server in-process
```

### Tools

| tool | args | notes |
|---|---|---|
| `charge_card` | `customer: str, amount: float` (dollars) | inserts a row, returns `{"charge_id": "ch_…", "status": "succeeded", …}`; annotated not read-only, not idempotent |
| `list_charges` | `customer: str` | that customer's charges, newest first; annotated `readOnlyHint: true` |
| `send_email` | `to, subject, body: str` | inserts into `emails`; not read-only, not idempotent; no tool reads emails back (on purpose) |

### Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env   # then put your key in ANTHROPIC_API_KEY
```

### Run

```bash
.venv/bin/python -m pytest -q                              # server test
.venv/bin/python -m agent.loop                             # REPL (type exit or Ctrl-D to quit)
.venv/bin/python -m agent.loop "charge customer 881 \$50"  # one-shot
.venv/bin/python scripts/show_charges.py
```

Set `DOUBLETAKE_DB=/path/to.db` to point the server, agent and `show_charges.py` at a different database.

### Trace format

`logs/<run_id>.jsonl`, one event per line. Every line has `run_id`, `seq`, `ts`, `event`.

| event | extra fields |
|---|---|
| `run_start` | `model`, `system`, `tools` (incl. MCP annotations) |
| `user_message` | `text` |
| `model_turn` | `turn`, `model`, `request_id`, `stop_reason`, `latency_ms`, `usage`, `content` |
| `tool_call` | `tool_use_id`, `name`, `input` |
| `tool_result` | `tool_use_id`, `name`, `is_error`, `output`, `duration_ms` |
| `tool_timeout` | `tool_use_id`, `name`, `timeout_s`, `duration_ms` |
| `api_error` | `error`, `rolled_back_to` |
| `turn_cap_hit` / `run_end` (`usage`) | |

## Phase 1: fault switches

Faults live in a JSON file (`DOUBLETAKE_FAULTS`, default `kitchen/faults.json`), re-read on every tool call:

```json
{
  "lose_reply":   {"enabled": true, "tools": ["charge_card", "send_email"], "hang_seconds": 30, "times": 1},
  "pending":      {"enabled": true, "pending_seconds": 30, "times": 1},
  "lookup_fails": {"enabled": true, "hang_seconds": 30, "times": 1}
}
```

- `lose_reply`: the row is committed first, then the tool sleeps `hang_seconds`, so the agent times out. The row survives the client cancelling.
- `pending`: `list_charges` hides charges younger than `pending_seconds`.
- `lookup_fails`: `list_charges` sleeps `hang_seconds` before answering.
- `times: N`: only the first N matching calls are affected (counter in server memory; omit for every call).

Tools are async (`asyncio.sleep`), so the server keeps answering while one call hangs.

The agent gives each tool call `DOUBLETAKE_TOOL_TIMEOUT` seconds (default 10). On timeout the model gets
`is_error: true`, `"timeout: no response from payments service"`. There is no retry logic, and the system prompt is unchanged.
`run_conversation(user_turns, db_path, faults_path, model)` in `agent/loop.py` runs a scripted conversation and returns a `RunSummary`.

```bash
.venv/bin/python scripts/demo_double_charge.py --runs 5 --model claude-opus-5-5
```

### Next phases

`dispatch_tool_call()` in `agent/loop.py` is the single place side effects happen; later phases add a guard in front of it.
