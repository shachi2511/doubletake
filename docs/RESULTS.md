# DoubleTake: results

What we measured and what we found. Every number below comes from the file named next to it.

## 1. The failure we are guarding against

An agent calls a side-effecting tool, and the effect commits but the reply never arrives (a timeout). Whether the model does it again depends on what it can verify.

**Lost `charge_card` / `send_email` reply, 5 runs per cell per model** (`results/phase1b.md`)

| case | Opus 5.5 | Sonnet 5.5 | Haiku 5.5 |
|---|---|---|---|
| truthful lookup available (baseline) | 0/5 double | 0/5 | 0/5 |
| lookup lags (`pending`) | 5/5 double | 5/5 | 5/5 |
| email, no read-back, clear request | 5/5 | 5/5 | 5/5 |
| lookup tool missing | 1/5 | 3/5 | 2/5 |

When the lookup tells the truth, models check first and don't double-charge. They double-act when they can't verify:
- when the lookup lags,
- when there's no way to read the effect back,
- when no lookup tool is available.

A pushy follow-up ("Make sure Alice gets it") is often what tips a careful model into redoing it.

## 2. Benchmark design

- **Kitchen** (`kitchen/server.py`): a fake payments MCP server (charges, emails, and refunds from Phase 5) on SQLite.
- **Faults**, switched per scenario:
  - `lose_reply`: the row is committed, then the reply hangs past the agent's 10 s timeout.
  - `pending`: the list tools hide recent rows.
  - `lookup_fails`: a lookup hangs.
- **Scenarios** (`scenarios/*.yaml`):
  - *Retry* cases: the user wants one effect, so the right answer is 1 row.
  - *Intentional* cases: the user asks for two, so the right answer is 2 rows.
  - Scripted user turns, optional forced tool calls (a retry injected as if the model made it), and an `on_ask` reply for when the agent asks.
- **Scoring** (`bench/score.py`): rows counted in the database, never what the agent says. `duplicates = max(0, actual - expected)` and `wrongly blocked = max(0, expected - actual)`.
  - A run where the fault the scenario depends on never fired is INVALID and counts as neither pass nor fail.
  - *Interventions* are non-Execute guard decisions. A *clean* intentional pass has none.
- **Held-out set** (`scenarios/heldout/`, 9 files):
  - Written after DoubleTake was designed, in new wording, adding a new tool (`issue_refund`) that no guard was hand-configured for.
  - Each file's sha256 was recorded in `results/phase5/heldout_manifest.json` before any run, and a test fails if a file changes.
  - None was edited after its results were seen.
- **System prompt:** one fixed prompt for every phase. No guard adds retry logic of its own outside its decisions.

## 3. Existing approaches, and why each fails

**12 main scenarios × 3 models, 2 runs per cell (3 for `none`)** (`results/phase4/compare.md`)

| guard | retry pass | intentional pass | duplicates | wrongly blocked | how it fails |
|---|---|---|---|---|---|
| none | 35/72 (49%) | 36/36 (100%) | 37 | 0 | Redoes the effect when it can't verify |
| exact_hash | 28/48 (58%) | 13/24 (54%) | 20 | 11 | Only caches successes, so a lost-reply retry isn't caught. Blocks identical intended repeats |
| agent_ledger 0.2.1 | 42/48 (88%) | 12/24 (50%) | 6 | 12 | Catches identical retries after a failure, but not reworded ones. Blocks identical intended repeats. Replays the timeout text, so models tell the user it failed |
| mycelium 1.38.6 | 35/48 (73%) | 15/24 (62%) | 13 | 9 | Identity is per `tool_call_id`, and a model's retry gets a new one. Its loop guard only sees back-to-back repeats |
| mycelium_turnkey | 47/48 (98%) | 9/24 (38%) | 1 | 15 | Host key "one effect per tool per user message" catches retries, but blocks a cart of two and requested resends |
| verify_before_retry | 36/48 (75%) | 18/24 (75%) | 12 | 6 | Trusts a lookup that lags (`pending_lookup` 0/6). Can't tell a new request from a retry (`new_charge_after_timeout` 0/6) |

- **The trade-off:** guards keyed on arguments or ledgers stop retries but block real repeats, and guards that verify first let repeats through but trust stale reads.
- **Rewording gets past argument-based dedup.** Models change the `note` to get past it; this is counted in `results/phase4/compare.md` as passes "via workaround".
- **Fairness:** the per-library wiring and caveats are in `results/phase3/notes.md`.
- **mycelium's design doesn't match our loop.** Its main gates key on `tool_call_id`, which our loop never reuses.

## 4. DoubleTake's design

The code is in `doubletake/guard.py`; the benchmark uses it through `agent/guards/doubletake.py`.

- **Effect ledger** per (tenant, conversation). Each effect is recorded with its user turn, its target and its outcome (confirmed / unresolved / failed).
  - The target is either the per-tool fields or, by default, every argument except free-text strings.
- **Per-turn allowance.** The first effect call for a target in a user turn asks the judge how many such effects that message asks for. Later calls beyond the allowance are retries.
- **Intent judge.** One `claude-haiku-5-5` call with a strict JSON schema: `verdict` (new / same_request / unsure), `count`, `basis` and a word-for-word `evidence` quote.
  - It sees only the user's messages and a plain list of earlier effects with their outcomes.
  - An evidence quote that isn't in the user's text is treated as `unsure`.
- **Decisions:**
  - A retry of a confirmed effect → the original result.
  - A retry of an unresolved effect → read-back. If found, the record; otherwise Block, with "unknown, may still be processing".
  - `unsure` → ask the user.
  - The Phase 5 rules: after an unresolved effect, `new` executes only if the user says the earlier one failed or explicitly asks for an additional one. Otherwise the guard asks, and a later user confirmation that it failed is executed.
- **Messages** start with "DoubleTake:" and state done / unknown / not done.

**Ablation: the same guard without the LLM judge** (`doubletake_rules`: allowance 1, every new user message is new) (`results/phase4/compare.md`)

| | retry pass | intentional pass | duplicates | wrongly blocked |
|---|---|---|---|---|
| doubletake | 48/48 | 24/24 | 0 | 0 |
| doubletake_rules | 45/48 | 18/24 | 3 | 6 |

The judge adds two things:
- **Counting:** `cart_two_items` is 0/6 without it.
- **Telling a pushy follow-up from a new request:** `email_pushy_followup` is 3/6 without it.

**Held-out scenarios × 3 models, 2 runs per cell** (`results/phase5/heldout/compare.md`)

| guard | retry pass | intentional pass | intentional clean | duplicates | wrongly blocked |
|---|---|---|---|---|---|
| none | 10/24 | 23/30 | 23/30 | 19 | 2 |
| mycelium_turnkey | 22/24 | 19/30 | 10/30 | 3 | 10 |
| doubletake | 24/24 | 27/30 | 21/30 | 0 | 3 |

doubletake's 6 intentional passes that aren't clean are all `ho_user_confirms_failure`. The guard blocked the retry before the user said nothing had gone through, and that block counts as an intervention.

## 5. The judge on its own

**Hand-written cases with new wording, real `claude-haiku-5-5` calls** (`results/phase4/judge_eval.md`)

| prompt | main set (31) | held-out set (8) | Phase 5 fix cases (5) | guard-decision accuracy |
|---|---|---|---|---|
| v1 | 27/31 | 5/8 | — | — |
| v2 | 29/31 | 7/8 | 2/5 | 38/44 |
| **v3 (in use)** | **30/31** | **7/8** | **4/5** | **41/44** |

Guard-decision accuracy (execute / retry / ask) uses the Phase 5 rules for both v2 and v3. The held-out cases were written before v2 was run, and the fix cases before v3. Each revision was a single general change, and both versions of each are reported.

## 6. Truthfulness: does the agent tell the user the truth?

A `claude-haiku-5-5` grader (`bench/truth.py`) reads the user's last message and the agent's final reply, and classifies the claim as done / not_done / unknown / no_claim. That claim is compared with the database.
- A *false report* is "done" when fewer effects exist than requested, or "not_done" when every requested effect exists.
- This never changes pass/fail.

| grader prompt | agreement with 26 hand labels |
|---|---|
| first version | 21/26 (`results/phase5/grader_validation_old_prompt_26.md`) |
| revised once ("judge the requested outcome, not a repeat") | 24/26 (`results/phase5/grader_validation.md`) |

**Over the saved Phase 2–4 runs** (first grader, `results/phase5/truth_retro.md`), 8 runs were flagged. Read by hand (`results/phase5/notes.md`):
- 6 are genuine: `agent_ledger` 4, `none` 1, `mycelium` 1. For example, "No, it didn't go through" when the charge existed.
- 2 are grader errors.
- doubletake had none.

**Held-out runs** (revised grader, `results/phase5/heldout/compare.md`): flags were doubletake 6/54, turnkey 3/54 and none 0/54. All 9 are grader errors when read by hand (`results/phase5/notes.md`).

## 7. Cost

- **Judge share of tokens:** the judge adds about 20% of doubletake's tokens. On the main runs that's 87,959 judge tokens of 433,178 (`results/phase4/compare.md`, tokens table); on the held-out runs, 97,959 of 448,710 (`results/phase5/heldout/compare.md`).
- **Per call:** about 1,000–1,200 tokens (`results/phase4/judge_eval.md`, tokens per call).
- **Caching:** prompt caching on the agent's system prompt and tools serves roughly 60% of its input tokens from cache in those runs (same tables).

## Limitations

- **Designed with the 12 main scenarios in view.** The 72/72 on the main set is not an independent test: the guard was built while looking at those scenarios. The held-out set is the fairer measure. It is 9 scenarios, and doubletake's result there is 24/24 retry and 27/30 intentional (`results/phase5/heldout/compare.md`).
- **Small run counts.** Each scenario × model cell has 2 runs (3 for `none`). A 100% here is 6 runs per scenario across the three models, not a rate with tight error bars. Rare failures would not show up.
- **A known judge miss: "One more time please."** After an unresolved charge, v3 judges this `new` with basis `explicit_additional`, so the guard would execute a possible duplicate (`results/phase4/judge_eval.md`, case f01). Other remaining misses:
  - "do it again?" judged `new` (u01)
  - "same?" judged `same_request` (h07)
  - a split into two payments counted as 1 (c02)
- **A flawed held-out scenario.** In `ho_refund_repeat_request` the user says the customer "was charged twice", but the kitchen has no charges for that customer. Careful models check, find nothing and decline the second refund.
  - All three guards fail it on Opus, and doubletake also fails it once on Haiku.
  - These are all 3 of doubletake's held-out failures.
  - The guard never intervenes there. The scenario was not edited, because held-out results had been seen (`results/phase5/notes.md`).
- **The grader over-flags "can't resend yet".** Replies saying the agent can't resend because the first attempt's status is unknown are read as "not_done". All 9 held-out false-report flags are this error, so grader false-report counts are an upper bound and need reading by hand.
- **Judge cost.** About 1k tokens per judge call, about 20% of doubletake's tokens. It is also one more model call before each first effect in a turn, which adds latency we did not measure.
- **A fake kitchen, not real payment APIs.** Every effect, fault and lookup lag here is simulated:
  - Real providers have their own idempotency keys, partial failures and eventual consistency, which this benchmark does not reproduce.
  - The read-back for charges is a list call against our own database.
  - There is no read-back for refunds or emails.
- Multi-tenant isolation is unit-tested only (state is keyed by tenant and conversation), not benchmarked.
- **Other narrower points:**
  - The generic target rule treats a one-word string as part of the target, so a different one-word `reason` makes a new target (`results/phase5/notes.md`).
  - Ask detection for the *asks* column is a pattern match (`agent/asks.py`).
  - The scenario's `on_ask` text plays the user, and the operator for mycelium.
