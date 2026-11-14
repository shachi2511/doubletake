"""Compare guards: results/phase3/compare.md.

    python -m bench.compare            # none (results/phase2) + every guard in results/phase3

Rebuilds every run from its saved row + trace (bench.report.load_runs), so all
guards are scored by the same code.
"""

import argparse
import sys
from datetime import datetime
from pathlib import Path

from agent.loop import ROOT
from bench.driver import RunResult
from bench.report import agg, frac, load_runs, rel
from bench.scenario import load_scenarios

PHASE2 = ROOT / "results" / "phase2"
PHASE3 = ROOT / "results" / "phase3"
DEFAULT_GUARDS = ["none", "exact_hash", "agent_ledger", "mycelium", "mycelium_turnkey", "verify_before_retry"]


def runs_file(guard: str) -> Path:
    return (PHASE2 if guard == "none" else PHASE3) / f"{guard}_runs.json"


def pct(k: int, n: int) -> str:
    return f"{k}/{n} ({100 * k / n:.0f}%)" if n else "—"


def overview_row(label: str, rs: list[RunResult]) -> str:
    a = agg(rs)
    ar = agg([r for r in rs if r.scenario.category == "retry"])
    ai = agg([r for r in rs if r.scenario.category == "intentional"])
    inv = f"{a['invalid']}" + (f" + {a['error']} err" if a["error"] else "")
    return (f"| {label} | {a['runs']} | {pct(ar['pass'], ar['valid'])} | {pct(ai['pass'], ai['valid'])} "
            f"| {pct(ai['clean'], ai['valid'])} | **{pct(a['pass'], a['valid'])}** | {a['duplicates']} "
            f"| {a['blocked']} | {a['asks']} | {a['interventions']} | {inv} | {a['tokens']:,} |")


OVERVIEW_HEAD = [
    "| guard | runs | retry pass | intentional pass | intentional clean pass | all pass | duplicates "
    "| wrongly blocked | asks | interventions | invalid | tokens |",
    "|---|---|---|---|---|---|---|---|---|---|---|---|",
]


def build(guards: list[str]) -> str:
    scenarios = load_scenarios()
    by_id = {s.id: s for s in scenarios}
    data: dict[str, list[RunResult]] = {}
    missing = []
    for g in guards:
        f = runs_file(g)
        if f.exists():
            data[g] = load_runs(f, by_id)
        else:
            missing.append(g)
    guards = [g for g in guards if g in data]
    models = list(dict.fromkeys(r.model for g in guards for r in data[g]))
    total = sum(r.summary.usage.total for g in guards for r in data[g])

    def rate(g, sc):
        a = agg([r for r in data[g] if r.scenario.id == sc.id])
        return a["pass"], a["valid"]

    md = [
        "# DoubleTake Phase 3 — guard comparison",
        "",
        f"- Generated: {datetime.now().isoformat(timespec='seconds')}",
        f"- Guards: {', '.join(f'`{g}`' for g in guards)}" + (f" (no results yet: {', '.join(missing)})" if missing else ""),
        f"- Models: {', '.join(f'`{m}`' for m in models)}",
        "- Runs per scenario × model: `none` 3 (Phase 2, `results/phase2/none_runs.json`); every other guard 2.",
        "  Rates are over valid runs, so compare percentages, not counts.",
        "- Same 12 scenarios, scorer and system prompt for every guard. Wiring and fairness caveats: "
        "[notes.md](notes.md).",
        "- `none`: the two email scenarios were re-run with the tool-neutral timeout text; the other 90 `none` runs "
        "used the old \"payments service\" text (see README). All Phase 3 runs use the new text.",
        f"- Tokens across all guards shown: **{total:,}**",
        "",
        "*Interventions* = non-Execute guard decisions on the scenario's effect tool (they never change pass/fail). "
        "*Intentional clean pass* = intentional run that passed with zero interventions; a pass with interventions "
        "means the model got past the guard (e.g. by changing the note).",
        "",
        "## Overview (all models)",
        "",
        *OVERVIEW_HEAD,
    ]
    for g in guards:
        md.append(overview_row(f"`{g}`", data[g]))

    md += ["", "## Overview per model", ""]
    for m in models:
        md += [f"### `{m}`", "", *OVERVIEW_HEAD]
        for g in guards:
            md.append(overview_row(f"`{g}`", [r for r in data[g] if r.model == m]))
        md.append("")

    md += ["## Pass rate by scenario (all models summed)", "",
           "Cells: passes / valid runs. Intentional cases also show clean passes (no guard intervention).", "",
           "| scenario | category | " + " | ".join(f"`{g}`" for g in guards) + " |",
           "|---|---|" + "---|" * len(guards)]
    for sc in scenarios:
        cells = []
        for g in guards:
            rs = [r for r in data[g] if r.scenario.id == sc.id]
            a = agg(rs)
            cell = frac(a["pass"], a["valid"])
            if sc.category == "intentional" and a["valid"]:
                cell += f" (clean {a['clean']})"
            if a["invalid"] or a["error"]:
                cell += f" [{a['invalid'] + a['error']} inv/err]"
            cells.append(cell)
        md.append(f"| {sc.id} | {sc.category} | " + " | ".join(cells) + " |")

    md += ["", "## Fixes and breaks compared with `none`", "",
           "Per scenario, all models summed. *Fixes*: higher pass rate than `none`. *Breaks*: lower pass rate "
           "than `none` (it passed more often unguarded). Intentional passes that needed a workaround are listed "
           "separately because the guard did intervene.", ""]
    for g in guards:
        if g == "none":
            continue
        fixes, breaks, wk = [], [], []
        for sc in scenarios:
            (pn, vn), (pg, vg) = rate("none", sc), rate(g, sc)
            if not vn or not vg:
                continue
            rn, rg = pn / vn, pg / vg
            label = f"{sc.id} ({pn}/{vn} → {pg}/{vg})"
            if rg > rn:
                fixes.append(label)
            elif rg < rn:
                breaks.append(label)
            w = [r for r in data[g] if r.scenario.id == sc.id and r.score.workaround]
            if w:
                wk.append(f"{sc.id} ({len(w)} of {pg} passes)")
        md += [f"### `{g}`", "",
               "- **Fixes:** " + ("; ".join(fixes) or "none"),
               "- **Breaks:** " + ("; ".join(breaks) or "none"),
               "- **Intentional passes via workaround:** " + ("; ".join(wk) or "none"), ""]

    odd = [(g, r) for g in guards for r in data[g] if r.score.status in ("invalid", "error")]
    md += [f"## Invalid and error runs ({len(odd)})", ""]
    if not odd:
        md.append("None.")
    for g, r in odd:
        md.append(f"- `{g}` {r.scenario.id} / `{r.model}` run {r.n}: {r.score.status} — `{rel(r.summary.trace_path)}`")
    md += ["", "Per-guard details (every failing run with its trace, all replies): "
           + ", ".join(f"[{g}]({'../phase2/' if g == 'none' else ''}{g}.md)" for g in guards), ""]
    return "\n".join(md)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--guards", default=",".join(DEFAULT_GUARDS))
    args = ap.parse_args()
    out = PHASE3 / "compare.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build([g.strip() for g in args.guards.split(",") if g.strip()]))
    print(f"written: {rel(out)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
