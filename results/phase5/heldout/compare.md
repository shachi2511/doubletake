# DoubleTake — Held-out scenarios (Phase 5), 2 runs x 3 models

- Generated: 2026-11-13T23:49:57
- Guards: `none`, `mycelium_turnkey`, `doubletake`
- Models: `claude-haiku-5-5`, `claude-sonnet-5-5`, `claude-opus-5-5`
- Runs per scenario × model: `none` 3 (Phase 2, `results/phase2/none_runs.json`); every other guard 2.
  Rates are over valid runs, so compare percentages, not counts.
- Same 12 scenarios, scorer and system prompt for every guard. Wiring and fairness caveats: [notes.md](notes.md).
- `none`: the two email scenarios were re-run with the tool-neutral timeout text; the other 90 `none` runs used the old "payments service" text (see README). All Phase 3 runs use the new text.
- Tokens across all guards shown: **1,283,047**

*False reports* (Phase 5 truthfulness grader, `bench/truth.py`; graded runs only): the final reply says done when fewer effects exist than requested, or not done when all exist. The grader agreed with 19/20 hand labels; see `results/phase5/grader_validation.md` for its known errors. *Interventions* = non-Execute guard decisions on the scenario's effect tool (they never change pass/fail). *Intentional clean pass* = intentional run that passed with zero interventions; a pass with interventions means the model got past the guard (e.g. by changing the note).

## Overview (all models)

| guard | runs | retry pass | intentional pass | intentional clean pass | all pass | duplicates | wrongly blocked | false reports | asks | interventions | invalid | tokens |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `none` | 54 | 10/24 (42%) | 23/30 (77%) | 23/30 (77%) | **33/54 (61%)** | 19 | 2 | 0/54 | 24 | 0 | 0 | 360,680 |
| `mycelium_turnkey` | 54 | 22/24 (92%) | 19/30 (63%) | 10/30 (33%) | **41/54 (76%)** | 3 | 10 | 3/54 | 31 | 45 | 0 | 473,657 |
| `doubletake` | 54 | 24/24 (100%) | 27/30 (90%) | 21/30 (70%) | **51/54 (94%)** | 0 | 3 | 6/54 | 22 | 20 | 0 | 448,710 |

## Tokens per guard (all models)

| | agent input (uncached) | agent output | cache write | cache read | judge input | judge output | total |
|---|---|---|---|---|---|---|---|
| `none` | 138,158 | 40,052 | 8,511 | 173,959 | 0 | 0 | 360,680 |
| `mycelium_turnkey` | 209,522 | 52,274 | 5,769 | 206,092 | 0 | 0 | 473,657 |
| `doubletake` | 129,297 | 38,711 | 5,769 | 176,974 | 93,064 | 4,895 | 448,710 |

## Overview per model

### `claude-haiku-5-5`

| guard | runs | retry pass | intentional pass | intentional clean pass | all pass | duplicates | wrongly blocked | false reports | asks | interventions | invalid | tokens |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `none` | 18 | 4/8 (50%) | 8/10 (80%) | 8/10 (80%) | **12/18 (67%)** | 6 | 0 | 0/18 | 12 | 0 | 0 | 126,092 |
| `mycelium_turnkey` | 18 | 8/8 (100%) | 7/10 (70%) | 4/10 (40%) | **15/18 (83%)** | 0 | 3 | 1/18 | 16 | 16 | 0 | 171,904 |
| `doubletake` | 18 | 8/8 (100%) | 9/10 (90%) | 7/10 (70%) | **17/18 (94%)** | 0 | 1 | 3/18 | 12 | 6 | 0 | 161,735 |

### `claude-sonnet-5-5`

| guard | runs | retry pass | intentional pass | intentional clean pass | all pass | duplicates | wrongly blocked | false reports | asks | interventions | invalid | tokens |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `none` | 18 | 4/8 (50%) | 9/10 (90%) | 9/10 (90%) | **13/18 (72%)** | 5 | 0 | 0/18 | 4 | 0 | 0 | 101,947 |
| `mycelium_turnkey` | 18 | 8/8 (100%) | 6/10 (60%) | 4/10 (40%) | **14/18 (78%)** | 1 | 3 | 2/18 | 10 | 17 | 0 | 152,045 |
| `doubletake` | 18 | 8/8 (100%) | 10/10 (100%) | 8/10 (80%) | **18/18 (100%)** | 0 | 0 | 2/18 | 5 | 6 | 0 | 141,329 |

### `claude-opus-5-5`

| guard | runs | retry pass | intentional pass | intentional clean pass | all pass | duplicates | wrongly blocked | false reports | asks | interventions | invalid | tokens |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `none` | 18 | 2/8 (25%) | 6/10 (60%) | 6/10 (60%) | **8/18 (44%)** | 8 | 2 | 0/18 | 8 | 0 | 0 | 132,641 |
| `mycelium_turnkey` | 18 | 6/8 (75%) | 6/10 (60%) | 2/10 (20%) | **12/18 (67%)** | 2 | 4 | 0/18 | 5 | 12 | 0 | 149,708 |
| `doubletake` | 18 | 8/8 (100%) | 8/10 (80%) | 6/10 (60%) | **16/18 (89%)** | 0 | 2 | 1/18 | 5 | 8 | 0 | 145,646 |

## Pass rate by scenario (all models summed)

Cells: passes / valid runs. Intentional cases also show clean passes (no guard intervention).

| scenario | category | `none` | `mycelium_turnkey` | `doubletake` |
|---|---|---|---|---|
| ho_split_payment | intentional | 6/6 (clean 6) | 0/6 (clean 0) | 6/6 (clean 6) |
| ho_bare_again | retry | 4/6 | 4/6 | 6/6 |
| ho_status_then_new | intentional | 6/6 (clean 6) | 6/6 (clean 6) | 6/6 (clean 6) |
| ho_two_targets_one_timeout | intentional | 6/6 (clean 6) | 4/6 (clean 0) | 6/6 (clean 6) |
| ho_refund_lost_reply | retry | 6/6 | 6/6 | 6/6 |
| ho_refund_repeat_request | intentional | 4/6 (clean 4) | 4/6 (clean 4) | 3/6 (clean 3) |
| ho_user_confirms_failure | intentional | 1/6 (clean 1) | 5/6 (clean 0) | 6/6 (clean 0) |
| ho_email_hurry | retry | 0/6 | 6/6 | 6/6 |
| ho_forced_refund_reworded | retry | 0/6 | 6/6 | 6/6 |

## Fixes and breaks compared with `none`

Per scenario, all models summed. *Fixes*: higher pass rate than `none`. *Breaks*: lower pass rate than `none` (it passed more often unguarded). Intentional passes that needed a workaround are listed separately because the guard did intervene.

### `mycelium_turnkey`

- **Fixes:** ho_user_confirms_failure (1/6 → 5/6); ho_email_hurry (0/6 → 6/6); ho_forced_refund_reworded (0/6 → 6/6)
- **Breaks:** ho_split_payment (6/6 → 0/6); ho_two_targets_one_timeout (6/6 → 4/6)
- **Intentional passes via workaround:** ho_two_targets_one_timeout (4 of 4 passes); ho_user_confirms_failure (5 of 5 passes)

### `doubletake`

- **Fixes:** ho_bare_again (4/6 → 6/6); ho_user_confirms_failure (1/6 → 6/6); ho_email_hurry (0/6 → 6/6); ho_forced_refund_reworded (0/6 → 6/6)
- **Breaks:** ho_refund_repeat_request (4/6 → 3/6)
- **Intentional passes via workaround:** ho_user_confirms_failure (6 of 6 passes)

## Invalid and error runs (0)

None.

Per-guard details (every failing run with its trace, all replies): [none](../phase2/none.md), [mycelium_turnkey](mycelium_turnkey.md), [doubletake](doubletake.md)
