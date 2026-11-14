# DoubleTake

Research project on duplicate side effects in AI agents.

## Phase 0: baseline agent + fake payments server

A Claude agent (`agent/loop.py`) calls a fake payments MCP server (`kitchen/server.py`) over stdio.
The server stores charges in SQLite at `data/payments.db`.

```
kitchen/server.py       fake payments MCP server (MCP Python SDK 2.x, MCPServer)
agent/loop.py           agent loop + REPL; tool calls go through dispatch_tool_call()
scripts/show_charges.py prints the charges table
data/                   SQLite db (git-ignored)
logs/                   JSONL traces, one file per run (git-ignored)
tests/                  pytest, connects to the server in-process
```

### Tools

| tool | args | notes |
|---|---|---|
| `charge_card` | `customer: str, amount: float` (dollars) | inserts a row, returns `{"charge_id": "ch_…", "status": "succeeded", …}`; annotated not read-only, not idempotent |
| `list_charges` | `customer: str` | that customer's charges, newest first; annotated `readOnlyHint: true` |

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
| `api_error` / `turn_cap_hit` / `run_end` | |

### Next phases

`dispatch_tool_call()` in `agent/loop.py` is the single place side effects happen; later phases add a guard in front of it.
