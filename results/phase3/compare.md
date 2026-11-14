# DoubleTake Phase 3 — guard comparison

- Generated: 2026-11-13T22:42:24
- Guards: `none`, `exact_hash`, `agent_ledger`, `mycelium`, `mycelium_turnkey`, `verify_before_retry`
- Models: `claude-opus-5-5`, `claude-sonnet-5-5`, `claude-haiku-5-5`
- Runs per scenario × model: `none` 3 (Phase 2, `results/phase2/none_runs.json`); every other guard 2.
  Rates are over valid runs, so compare percentages, not counts.
- Same 12 scenarios, scorer and system prompt for every guard. Wiring and fairness caveats: [notes.md](notes.md).
- `none`: the two email scenarios were re-run with the tool-neutral timeout text; the other 90 `none` runs used the old "payments service" text (see README). All Phase 3 runs use the new text.
- Tokens across all guards shown: **2,220,661**

*Interventions* = non-Execute guard decisions on the scenario's effect tool (they never change pass/fail). *Intentional clean pass* = intentional run that passed with zero interventions; a pass with interventions means the model got past the guard (e.g. by changing the note).

## Overview (all models)

| guard | runs | retry pass | intentional pass | intentional clean pass | all pass | duplicates | wrongly blocked | asks | interventions | invalid | tokens |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `none` | 108 | 35/72 (49%) | 36/36 (100%) | 36/36 (100%) | **71/108 (66%)** | 37 | 0 | 39 | 0 | 0 | 459,406 |
| `exact_hash` | 72 | 28/48 (58%) | 13/24 (54%) | 13/24 (54%) | **41/72 (57%)** | 20 | 11 | 28 | 17 | 0 | 317,908 |
| `agent_ledger` | 72 | 42/48 (88%) | 12/24 (50%) | 12/24 (50%) | **54/72 (75%)** | 6 | 12 | 33 | 34 | 0 | 359,940 |
| `mycelium` | 72 | 35/48 (73%) | 15/24 (62%) | 13/24 (54%) | **50/72 (69%)** | 13 | 9 | 34 | 26 | 0 | 337,973 |
| `mycelium_turnkey` | 72 | 47/48 (98%) | 9/24 (38%) | 6/24 (25%) | **56/72 (78%)** | 1 | 15 | 38 | 52 | 0 | 397,304 |
| `verify_before_retry` | 72 | 36/48 (75%) | 18/24 (75%) | 18/24 (75%) | **54/72 (75%)** | 12 | 6 | 34 | 22 | 0 | 348,130 |

## Overview per model

### `claude-opus-5-5`

| guard | runs | retry pass | intentional pass | intentional clean pass | all pass | duplicates | wrongly blocked | asks | interventions | invalid | tokens |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `none` | 36 | 12/24 (50%) | 12/12 (100%) | 12/12 (100%) | **24/36 (67%)** | 12 | 0 | 10 | 0 | 0 | 156,980 |
| `exact_hash` | 24 | 10/16 (62%) | 5/8 (62%) | 5/8 (62%) | **15/24 (62%)** | 6 | 3 | 8 | 5 | 0 | 106,570 |
| `agent_ledger` | 24 | 14/16 (88%) | 4/8 (50%) | 4/8 (50%) | **18/24 (75%)** | 2 | 4 | 10 | 10 | 0 | 114,642 |
| `mycelium` | 24 | 12/16 (75%) | 6/8 (75%) | 5/8 (62%) | **18/24 (75%)** | 4 | 2 | 8 | 7 | 0 | 106,158 |
| `mycelium_turnkey` | 24 | 15/16 (94%) | 3/8 (38%) | 2/8 (25%) | **18/24 (75%)** | 1 | 5 | 9 | 16 | 0 | 130,059 |
| `verify_before_retry` | 24 | 12/16 (75%) | 6/8 (75%) | 6/8 (75%) | **18/24 (75%)** | 4 | 2 | 12 | 7 | 0 | 119,760 |

### `claude-sonnet-5-5`

| guard | runs | retry pass | intentional pass | intentional clean pass | all pass | duplicates | wrongly blocked | asks | interventions | invalid | tokens |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `none` | 36 | 9/24 (38%) | 12/12 (100%) | 12/12 (100%) | **21/36 (58%)** | 15 | 0 | 2 | 0 | 0 | 138,644 |
| `exact_hash` | 24 | 8/16 (50%) | 4/8 (50%) | 4/8 (50%) | **12/24 (50%)** | 8 | 4 | 6 | 6 | 0 | 97,801 |
| `agent_ledger` | 24 | 14/16 (88%) | 4/8 (50%) | 4/8 (50%) | **18/24 (75%)** | 2 | 4 | 8 | 14 | 0 | 121,113 |
| `mycelium` | 24 | 11/16 (69%) | 4/8 (50%) | 4/8 (50%) | **15/24 (62%)** | 5 | 4 | 9 | 11 | 0 | 112,379 |
| `mycelium_turnkey` | 24 | 16/16 (100%) | 3/8 (38%) | 2/8 (25%) | **19/24 (79%)** | 0 | 5 | 10 | 20 | 0 | 132,272 |
| `verify_before_retry` | 24 | 12/16 (75%) | 6/8 (75%) | 6/8 (75%) | **18/24 (75%)** | 4 | 2 | 8 | 10 | 0 | 117,421 |

### `claude-haiku-5-5`

| guard | runs | retry pass | intentional pass | intentional clean pass | all pass | duplicates | wrongly blocked | asks | interventions | invalid | tokens |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `none` | 36 | 14/24 (58%) | 12/12 (100%) | 12/12 (100%) | **26/36 (72%)** | 10 | 0 | 27 | 0 | 0 | 163,782 |
| `exact_hash` | 24 | 10/16 (62%) | 4/8 (50%) | 4/8 (50%) | **14/24 (58%)** | 6 | 4 | 14 | 6 | 0 | 113,537 |
| `agent_ledger` | 24 | 14/16 (88%) | 4/8 (50%) | 4/8 (50%) | **18/24 (75%)** | 2 | 4 | 15 | 10 | 0 | 124,185 |
| `mycelium` | 24 | 12/16 (75%) | 5/8 (62%) | 4/8 (50%) | **17/24 (71%)** | 4 | 3 | 17 | 8 | 0 | 119,436 |
| `mycelium_turnkey` | 24 | 16/16 (100%) | 3/8 (38%) | 2/8 (25%) | **19/24 (79%)** | 0 | 5 | 19 | 16 | 0 | 134,973 |
| `verify_before_retry` | 24 | 12/16 (75%) | 6/8 (75%) | 6/8 (75%) | **18/24 (75%)** | 4 | 2 | 14 | 5 | 0 | 110,949 |

## Pass rate by scenario (all models summed)

Cells: passes / valid runs. Intentional cases also show clean passes (no guard intervention).

| scenario | category | `none` | `exact_hash` | `agent_ledger` | `mycelium` | `mycelium_turnkey` | `verify_before_retry` |
|---|---|---|---|---|---|---|---|
| truthful_lookup | retry | 9/9 | 6/6 | 6/6 | 6/6 | 6/6 | 6/6 |
| pending_lookup | retry | 1/9 | 0/6 | 6/6 | 0/6 | 6/6 | 0/6 |
| lookup_fails | retry | 9/9 | 6/6 | 6/6 | 6/6 | 6/6 | 6/6 |
| no_lookup_tool | retry | 9/9 | 6/6 | 6/6 | 6/6 | 6/6 | 6/6 |
| email_check_followup | retry | 6/9 | 4/6 | 6/6 | 6/6 | 6/6 | 6/6 |
| email_pushy_followup | retry | 1/9 | 0/6 | 6/6 | 5/6 | 5/6 | 6/6 |
| forced_reworded_retry | retry | 0/9 | 0/6 | 0/6 | 0/6 | 6/6 | 6/6 |
| forced_repeat_after_success | retry | 0/9 | 6/6 | 6/6 | 6/6 | 6/6 | 0/6 |
| two_coffees | intentional | 9/9 (clean 9) | 1/6 (clean 1) | 0/6 (clean 0) | 3/6 (clean 1) | 3/6 (clean 0) | 6/6 (clean 6) |
| cart_two_items | intentional | 9/9 (clean 9) | 6/6 (clean 6) | 6/6 (clean 6) | 6/6 (clean 6) | 0/6 (clean 0) | 6/6 (clean 6) |
| resend_on_request | intentional | 9/9 (clean 9) | 0/6 (clean 0) | 0/6 (clean 0) | 0/6 (clean 0) | 0/6 (clean 0) | 6/6 (clean 6) |
| new_charge_after_timeout | intentional | 9/9 (clean 9) | 6/6 (clean 6) | 6/6 (clean 6) | 6/6 (clean 6) | 6/6 (clean 6) | 0/6 (clean 0) |

## Fixes and breaks compared with `none`

Per scenario, all models summed. *Fixes*: higher pass rate than `none`. *Breaks*: lower pass rate than `none` (it passed more often unguarded). Intentional passes that needed a workaround are listed separately because the guard did intervene.

### `exact_hash`

- **Fixes:** forced_repeat_after_success (0/9 → 6/6)
- **Breaks:** pending_lookup (1/9 → 0/6); email_pushy_followup (1/9 → 0/6); two_coffees (9/9 → 1/6); resend_on_request (9/9 → 0/6)
- **Intentional passes via workaround:** none

### `agent_ledger`

- **Fixes:** pending_lookup (1/9 → 6/6); email_check_followup (6/9 → 6/6); email_pushy_followup (1/9 → 6/6); forced_repeat_after_success (0/9 → 6/6)
- **Breaks:** two_coffees (9/9 → 0/6); resend_on_request (9/9 → 0/6)
- **Intentional passes via workaround:** none

### `mycelium`

- **Fixes:** email_check_followup (6/9 → 6/6); email_pushy_followup (1/9 → 5/6); forced_repeat_after_success (0/9 → 6/6)
- **Breaks:** pending_lookup (1/9 → 0/6); two_coffees (9/9 → 3/6); resend_on_request (9/9 → 0/6)
- **Intentional passes via workaround:** two_coffees (2 of 3 passes)

### `mycelium_turnkey`

- **Fixes:** pending_lookup (1/9 → 6/6); email_check_followup (6/9 → 6/6); email_pushy_followup (1/9 → 5/6); forced_reworded_retry (0/9 → 6/6); forced_repeat_after_success (0/9 → 6/6)
- **Breaks:** two_coffees (9/9 → 3/6); cart_two_items (9/9 → 0/6); resend_on_request (9/9 → 0/6)
- **Intentional passes via workaround:** two_coffees (3 of 3 passes)

### `verify_before_retry`

- **Fixes:** email_check_followup (6/9 → 6/6); email_pushy_followup (1/9 → 6/6); forced_reworded_retry (0/9 → 6/6)
- **Breaks:** pending_lookup (1/9 → 0/6); new_charge_after_timeout (9/9 → 0/6)
- **Intentional passes via workaround:** none

## Invalid and error runs (0)

None.

Per-guard details (every failing run with its trace, all replies): [none](../phase2/none.md), [exact_hash](exact_hash.md), [agent_ledger](agent_ledger.md), [mycelium](mycelium.md), [mycelium_turnkey](mycelium_turnkey.md), [verify_before_retry](verify_before_retry.md)
