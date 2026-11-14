# Phase 5 notes

## Pre-registered before the held-out scenarios were written

**Generic target rule** for side-effecting tools with no per-tool config. Written down first, so it can't be fitted to the held-out scenarios:

> The target is every argument **except free-text strings**. A string counts as free text when its value contains whitespace or is longer than 40 characters. Ids, emails and amounts stay in the target; descriptions such as `reason: "item arrived damaged"` do not.

- `charge_card` (customer + amount) and `send_email` (to + subject) keep their per-tool config from Phase 4.
- `issue_refund` gets **no** per-tool config. It is the generalization test.
- Expected limitation: a one-word `reason` (no whitespace) stays in the target, so the same refund with a different one-word reason counts as a different target.

**No read-back for the new tool.** doubletake's read-back (`READERS`) is configured only for `charge_card` → `list_charges`. Nothing pairs `issue_refund` with `list_refunds`. So for an unresolved refund the guard cannot check and will report "unknown". This is also part of the test: we expect a block, not a lookup.

**The new tool is hidden from the 12 main scenarios.** `issue_refund` and `list_refunds` are opt-in tools. A scenario sees them only if it lists them in `extra_tools`.
- The main scenarios don't, so the model and the guards see exactly the 3 tools of Phases 2–4, with the same descriptions and cached prefix.
- This is the only schema extension: an optional `extra_tools` field, plus `refunds` as an `expect.table`.

**Baseline wiring for the new tool.** A real deployer of a baseline guard would configure a new tool, so:
- `mycelium` / `mycelium_turnkey`: `issue_refund` = `keyed_mutate`, `list_refunds` = `read`. No refund reconciler; the charge reconciler is unchanged.
- `exact_hash`, `agent_ledger` and `verify_before_retry` are not run in this phase.

**Kitchen faults:**
- `lose_reply` applies to any tool listed in its `tools`, including `issue_refund`.
- `pending` and `lookup_fails` apply to both list tools (`list_charges`, `list_refunds`).
