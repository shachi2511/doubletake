# Phase 3 notes: how each guard is wired

All guards run inside `dispatch_tool_call` through the Phase 2 interface:
`before_tool_call(ctx, call)` → `Execute | ReturnResult | Block | AskUser`, then
`after_tool_call(ctx, call, outcome)` only for executed calls.
- Every guard keeps its state per `(tenant_id, run_id)` (`Guard.state`).
- A fresh guard instance is created for every run.
- The scenarios, the scorer and the system prompt are unchanged from Phase 2.

## Interface addition: `ctx.lookup(tool, args) -> Outcome`

- **Mechanism:** an async call through the run's own MCP client, with the same timeout as model calls (10 s).
- **Visibility:** logged as a `guard_lookup` trace event and never shown to the model.
- **Excluded tools are refused.** `no_lookup_tool` hides `list_charges` from the model, so guards cannot use it either.
- **Mutating tools are refused.** Only tools the server annotates `readOnlyHint` (here only `list_charges`) are allowed.
- **Refusals** raise `LookupRefused` and are logged with status `refused`.

**Fairness:** guard lookups hit the same fault-injected server as the model.
- `pending` hides recent charges from guards too.
- A guard lookup counts against `lookup_fails` (`times: 1`). If a guard looks before the model does, the model's own lookup then works.

## exact_hash (ours, simplest dedup)

- **Key:** tool name + args normalized (sorted keys, numbers as floats, strings stripped).
- **Caching:** only successful results are cached; a repeat returns the cached text as `ReturnResult`.
- **Not cached:** timed-out or errored calls, so their identical retry executes.
- **Reads:** read-only tools always execute.

**Expected blind spots:**
- The lost-reply case itself, because the timed-out call is never cached.
- Any change to args, such as a `note`.
- Intentional identical repeats get the cached result back instead of a new charge.

## agent_ledger — `agent-ledger==0.2.1` (PyPI, Apache-2.0)

- **Source read:** the full source (`ledger.py`, `types.py`, `utils.py`, `stores/memory.py`) and the README. Dependencies are pydantic, cachetools and rfc8785. No network calls.
- **Store:** one `EffectLedger(EffectLedgerOptions(store=MemoryStore()))` per (tenant, run).
- **Call identity:** `ToolCall(workflow_id=run_id, tool=name, args=input, call_id=tool_use_id)`, with the library's default idempotency key: `sha256(workflow_id | tool | RFC 8785 canonical args)`.

**Fresh key** (`find_by_idem_key` returns nothing):
- `ledger.begin(call)`, then `Execute`.
- After the MCP call: `commit(CommitSucceeded(result=text))` on success, `commit(CommitFailed(EffectError(message=text)))` on timeout or error.

That is the README's "Fine-Grained Control" pattern (`except Exception → CommitFailed`). It is also what `run()` does when a handler raises.

**Known key:** we call the library's own `ledger.run(call, handler)`, which resolves the recorded effect without calling the handler.

| ledger state | library behaviour | our decision |
|---|---|---|
| SUCCEEDED | returns recorded result | `ReturnResult(result)` |
| FAILED | raises `EffectFailedError(original message)` | `Block(message)` |
| PROCESSING / approval wait times out | `EffectTimeoutError` | `Block(message)` |
| DENIED / CANCELED | `EffectDeniedError` | `Block(message)` |

**What had to be adapted:**
- `run()` wants to own execution (handler = the side effect). Our interface separates the decision from execution. So for fresh keys we use the documented `begin`/`commit` pair, and for known keys `run()` itself.
- **Read-only tools are not wrapped.** The README lists read-only agents under "Probably not needed", and replaying a recorded `list_charges` would hand the model stale data.

**Could be unfair to it:**
- With no approval policy configured, there is no ask path, so it never asks the user.
- The `FAILED` replay returns the original error text verbatim (`timeout: no response from the service`). The model then reads a blocked retry as a second timeout. In the smoke run Haiku told the user the charge "did not go through" when it had; the run still passed on DB count.
- Dedup is by exact args. A model that adds or changes `note` gets a new key.

## mycelium — `mycelium-runtime==1.38.6` (PyPI)

- **Docs read:** the README sections on manual integration, transition identity, resolution gates, side-effect classes, reconciler, operator runbook and loop guard.
- **Source read:** `loop_guard.py`, `reconcile.py`, and the claim / args-drift / `attach_external_operation_ref` parts of `action_ledger.py` and `transition.py`.
- **Prototype:** I checked the gates in-process before wiring.

**Wiring:**
- **Integration path:** the README's *Manual integration (claim → execute → complete)*, the documented path when the host owns the tool runner. Its decorators and `mycelium run` need to wrap the tool function itself, but here the tool runs in a separate MCP server process.
- **Per (tenant, run):**
  - `ActionLedger(storage=InMemoryLedgerStorage(), reconciler=ChargeReconciler(ctx), on_args_drift="soft")`. This turns the args-drift gate on, at its documented default.
  - `LoopGuard(InMemoryLoopGuardStorage(), missing_run_id_policy="error")`, with default thresholds: keyed/non-idempotent mutate 2, read 5, `escalate_after_soft` 1.
- **Bindings:**
  - `charge_card` = `keyed_mutate`
  - `send_email` = `non_idempotent_mutate`
  - `list_charges` = `read`
  - `agent_id` "doubletake", `policy_version` "phase3"
- **Scope:** every call runs inside `execution_scope(TransitionScope(thread_id=run_id, run_id=run_id, node="tools"))` with kwargs `tool_call_id=<our tool_use_id>`.

**Before a call**, in the README's wrapper order:
1. `LoopGuard.check`.
2. `derive_request_id`.
3. `claim_read_only_async` or `claim_side_effecting_async` (these poll in-flight peers themselves).
4. A `COMPLETED` entry becomes `ReturnResult`.
5. Otherwise `record_decision(allowed)`. For `charge_card`, also `attach_external_operation_ref` with `{customer, amount_cents, started_at}`. Then `Execute`.

**After a call:**
- `complete(result)` on success.
- `fail(failed_after_effect=True)` on timeout or error for mutating tools. The README says: "The provider may have accepted before raising: park ambiguity."
- `fail(failed_after_effect=False)` for reads.

**Gate mapping:**
- `ToolBoundaryError` (loop soft block, args drift) → `Block(llm_message)`
- `LedgerSoftBlockError` / `LedgerPollTimeoutError` → `Block`
- `LedgerHardBlockError` from the LoopGuard (whole run frozen) → `Block`
- `LedgerHardBlockError` from the ledger (ambiguous transition; operator release required) → `AskUser`
- "Poll" is handled inside the claim calls.

**Reconciler** (`charge_card` only): `reconcile_async` calls `ctx.lookup("list_charges")` and looks for a matching charge created at or after `started_at`.
- Found → `COMPLETED` with that charge.
- Not found, or the lookup failed → `UNKNOWN`. That fails closed, like the README's Gmail adapter on indexing lag, because absence from a lagging list is not proof of `NOT_EXECUTED`.
- There is no email reconciler, as specified.

**What had to be adapted:**
- **Manual mode instead of the decorators.** In manual mode the README says `side_effect()` / `record_external_operation()` are ignored, so we call `ledger.attach_external_operation_ref` directly.
- **The external ref is synthetic.** Our fake provider has no operation id or idempotency key, so the ref is the call's customer, amount and start time.

**Could be unfair to it (important):**
- **Identity is per `tool_call_id`.** The README says outright: "Mycelium does not infer 'this is a retry' from arguments alone … a new `tool_call_id` is a new transition."
  - Our model always mints a new `tool_use_id` when it retries.
  - So the ledger's RETURN / HARD_BLOCK gates and the reconciler only fire for a redispatch of the **same** `tool_use_id`. That never happens in this benchmark, because there is no framework redispatch.
  - The protection that does apply is the **loop guard**: identical tool+args across new ids, counted as *consecutive* identical actions. Any different call in between (for example the model's own `list_charges`) resets the streak.
  - The library's intended fix is a host-owned business `request_id` (e.g. `charge-order:ORD-123`). Our scenarios have no order id to give it, and inventing one per scenario would change the benchmark.
- **The loop guard soft-blocks intentional identical repeats** (two coffees back to back). Models then usually retry with a different `note`, and that charge goes through.
- **No operator in the loop.** "Operator release required" becomes `AskUser`, and the scenario's `on_ask` text plays the operator. We never call `ledger.release`.
- **In-memory storage** is fine within one process, which is all a run is. The library warns about it; that warning is silenced.

## mycelium_turnkey — same library and version, plus a host business `request_id`

This guard is identical to `mycelium` above, with one addition. For mutating tools (`charge_card`, `send_email`), the host passes the explicit `request_id` that the library's docs ask for ("derive it from a server-owned record, never from the model"):

```
request_id = f"{run_id}:user_turn_{N}:{tool}"
```

- **N** = the number of user-authored messages so far, counting `on_ask` replies but not tool results. So it is the user message that led to the call. A forced call gets the turn it was injected after.
- **Reads keep the derived identity.** A fixed `request_id` would make a second `list_charges` in the same turn replay the first, now stale, result.

**This key is our choice, not the library's.** It means "one effect per tool per user message". The library's README example is `charge-order:{order_id}`; our scenarios have no order id, so we use the user turn as the closest host-owned unit of intent.

With this key, a retry under a new `tool_use_id` in the same turn reaches the ledger's gates, which plain `mycelium` never does:
- After a timeout: `FAILED_AFTER_EFFECT` → reconciler.
  - Found charge → `RETURN` (`ReturnResult`).
  - Otherwise → `HARD_BLOCK` → `AskUser`.
- Same key, different args (e.g. a reworded `note`) → identity conflict → `ToolBoundaryError` → `Block`.
- Same key after success → `RETURN`. In practice the loop guard often fires first on a back-to-back identical repeat; the wrapper order puts the loop guard before the ledger.

**Downsides of this key:**
- **Two intended effects in one message share a key.** `cart_two_items` ("two coffees … as two separate charges") gets its second charge refused as an identity conflict or replayed, so it is wrongly blocked unless the model moves the second charge to another turn.
- **Retries across turns are not deduplicated.** If the model's retry comes after the user's follow-up message (a new N), the key is new and only the loop guard applies.
- **An email after a timeout** (no reconciler) hard-blocks, and any further send in the same turn is refused until the next user message.

## verify_before_retry (ours, after arXiv 2608.02645)

**Scope and trigger:**
- Only `charge_card` and `send_email` are guarded.
- It acts when an earlier similar call in this run ended in timeout or error. Similar means same customer + amount for a charge, or same to + subject for an email.

**Charge:**
- `ctx.lookup("list_charges")` for a matching charge created at or after the earlier call. Found → `ReturnResult(charge)`.
- Not found → wait 3 s and check once more. Still not found → `Execute`; that is the one allowed retry.
- Lookup failed or refused → `AskUser`.

**Email:** `AskUser`, because there is no read-back.

**Limits:**
- At most one retry per logical operation; further similar calls are `Block`ed.
- An earlier similar call that **succeeded** changes nothing (`Execute`), as in the paper. So it does not stop `forced_repeat_after_success`.
- It cannot read the user's answer to an `AskUser`, so it asks again if the model calls again.

**Known weaknesses:**
- **The ambiguity budget can be shorter than the lag.** `pending_lookup` hides charges for 20 s. The checks happen about 10 s and 13 s after the first call, so the retry is allowed and double-charges.
- **It cannot tell a retry from an intentional repeat.** In `new_charge_after_timeout`, the user's new $50 charge is matched to the earlier timed-out one and answered with the old charge.
