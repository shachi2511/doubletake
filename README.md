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
| `charge_card` | `customer: str, amount: float` (dollars), `note: str = ""` | inserts a row, returns `{"charge_id": "ch_…", "status": "succeeded", …}`; annotated not read-only, not idempotent |
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
`is_error: true`, `"timeout: no response from the service"`. There is no retry logic, and the system prompt is unchanged.

> **Change (2026-11-13, during Phase 2):** the timeout text used to be `"timeout: no response from payments service"` for every tool, including `send_email`. It is now tool-neutral. All Phase 1 / 1b results, and the Phase 2 runs other than the two email scenarios, were recorded with the old text. `email_check_followup` and `email_pushy_followup` in `results/phase2/none.md` were re-run with the new text.
`run_conversation(user_turns, db_path, faults_path, model)` in `agent/loop.py` runs a scripted conversation and returns a `RunSummary`.

```bash
.venv/bin/python scripts/demo_double_charge.py --runs 5 --model claude-opus-5-5
```

## Phase 2: test kitchen (guards + scenario suite)

```
agent/guards/      Guard interface (base.py), "none" guard, registry (GUARDS / get_guard)
agent/asks.py      ask-the-user detection patterns (shared with the Phase 1b demo)
bench/             scenario schema, driver, scoring, report, CLI
scenarios/         12 scenario files, NN_<id>.yaml
results/phase2/    <guard>.md report + <guard>_runs.json (one row per run)
```

Every tool call, whether the model's or a scenario's forced call, goes through
`guard.before_tool_call(ctx, call)` inside `dispatch_tool_call`.
- The guard returns `Execute`, `ReturnResult(text)`, `Block(message)` or `AskUser(question)`.
- `AskUser` does not run the tool. The model gets an `is_error` result `NEEDS_CONFIRMATION: <question>`, and the runner answers with the scenario's `on_ask`.
- `after_tool_call(ctx, call, outcome)` runs for executed calls, with outcome `succeeded`, `error` or `timeout`.
- `ctx` carries `run_id`, `tenant_id` (from config), the message history, and every earlier call with its decision, outcome and timestamp.
- Each decision is logged as a `guard_decision` trace event.

```bash
.venv/bin/python -m bench.run --guard none --models claude-haiku-5-5 --runs 1
.venv/bin/python -m bench.run --guard none --models claude-opus-5-5,claude-sonnet-5-5,claude-haiku-5-5 --runs 3
.venv/bin/python -m bench.run --guard none --models claude-haiku-5-5 --scenarios pending_lookup --runs 2 --merge
```

Scoring counts rows in the database.
- `pass`: the count equals `expect.effects`.
- `INVALID`: `requires_fault_hit` is set and the lost reply never fired.
- `error`: an API error cut the run short.
- Neither INVALID nor error runs count as a pass or a fail.

The full rules are in `bench/score.py`.

New trace events: `guard_decision`, `forced_tool_call`, `turn_skipped`, `scenario`, `score`.
`user_message` gains `source` (`script` | `on_ask`), and `tool_call` gains `forced`.

## Phase 3: existing approaches as guards

```bash
.venv/bin/pip install -r requirements-baselines.txt    # agent-ledger, mycelium-runtime (pinned)
.venv/bin/python -m bench.run --guard verify_before_retry --models claude-haiku-5-5 --runs 2 --results-dir results/phase3
.venv/bin/python -m bench.compare                     # -> results/phase3/compare.md
```

| guard | what it is |
|---|---|
| `exact_hash` | caches successful results by tool + normalized args; replays exact repeats |
| `agent_ledger` | `agent-ledger` 0.2.1: begin/commit per (run, tool, args); failed effects block identical retries |
| `mycelium` | `mycelium-runtime` 1.38.6, manual claim/complete: ledger + loop guard + args-drift gate + charge reconciler |
| `mycelium_turnkey` | same, plus a host `request_id` = `{run_id}:user_turn_{N}:{tool}` |
| `verify_before_retry` | ours, after arXiv 2608.02645: after a failed similar call, verify via `list_charges` (or ask, for email) |

Guards can call `ctx.lookup(tool, args)` for read-only tools the model also has. These calls are logged as `guard_lookup` and never shown to the model.

Reports also count *interventions* (non-Execute decisions on the effect tool) and *clean passes* (intentional runs that passed with no intervention). Wiring and fairness notes are in `results/phase3/notes.md`.

## Phase 4: the DoubleTake guard (intent judge)

`agent/guards/doubletake.py`. For every call to a side-effecting tool, the guard decides whether the user asked for it or whether it is a retry.

- **Effect ledger, per (tenant, run).** Each effect is recorded with its user turn, a *target* and an outcome.
  - Targets: `charge_card` uses customer + amount; `send_email` uses to + subject. Free text such as `note` and `body` is ignored, so rewording does not make a new effect.
  - Outcomes: `confirmed`, `unresolved` (a timeout or error that may have committed), or `failed` (the server rejected the arguments).
- **Allowance per user turn.** The first effect call for a target in a turn asks the judge how many such effects that message requests. Calls beyond the allowance are retries.
- **Intent judge.** One `claude-haiku-5-5` call with a strict JSON schema at low effort, returning `{"verdict": "new" | "same_request" | "unsure", "count", "evidence"}`.
  - It sees only the user's messages and a plain list of earlier effects with their outcomes.
  - An evidence quote that is not found word for word in a user message is treated as `unsure`.
- **Decisions:**
  - Within the allowance → execute.
  - Retry of a confirmed effect → return the original result.
  - Retry of an unresolved effect → check with the read tool. Found → return it. Not found or no read tool → block, because lookups can lag.
  - `unsure` about an earlier matching effect → ask the user.
- **Messages.** Every guard message starts with `DoubleTake:` and says whether the effect is done, its status is unknown, or it was not done. The service's timeout text is never replayed.
- **`doubletake_rules`** is the ablation with no judge: the allowance is always 1 and every new user turn counts as new.

```bash
.venv/bin/python -m bench.judge_eval --label v2          # judge alone, 39 hand-written cases -> results/phase4/judge_eval.md
.venv/bin/python -m bench.run --guard doubletake --models claude-haiku-5-5 --runs 2 --results-dir results/phase4
.venv/bin/python -m bench.compare --out results/phase4/compare.md --title "Phase 4 comparison"
```

**Cost controls**
- `agent/loop.py` sets prompt-cache breakpoints on the system prompt and the last tool definition. The 641-token prefix is above the 512-token minimum, so it is cached.
- Judge tokens are logged as `judge_call` events and kept as `judge_usage`, separate from the agent's tokens. They are included in every run total and report.

**Judge prompt history**
- v1 missed "try again" after a failure (it answered `new`) and two count cases.
- v2 is one general revision. Both versions are scored in `results/phase4/judge_eval.md`, on the main set and on a held-out set written before v2 was run.

### Next phases

`dispatch_tool_call()` in `agent/loop.py` is the single place side effects happen; later phases add a guard in front of it.
