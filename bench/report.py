"""Saved runs (results/phase2/<guard>_runs.json) and the Markdown report (<guard>.md)."""

import json
from datetime import datetime
from pathlib import Path

from agent.asks import ASK_PATTERNS, asks_user
from agent.loop import ROOT, summary_from_trace
from bench.driver import RunResult
from bench.scenario import Scenario
from bench.score import score

RESULTS_DIR = ROOT / "results" / "phase2"


def rel(p) -> str:
    try:
        return str(Path(p).resolve().relative_to(ROOT))
    except ValueError:
        return str(p)


# ---------------------------------------------------------------- saved runs


def save_runs(results: list[RunResult], path: Path) -> None:
    rows = [
        {"scenario": r.scenario.id, "model": r.model, "run": r.n, "guard": r.guard,
         "effects": r.score.effects, "status": r.score.status, "asks": r.score.asks,
         "interventions": r.score.interventions,
         "trace": rel(r.summary.trace_path)}
        for r in results
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows, indent=1) + "\n")


def load_runs(path: Path, scenarios: dict[str, Scenario]) -> list[RunResult]:
    """Rebuild results from the saved rows + traces; effects come from the saved DB count."""
    out = []
    for row in json.loads(Path(path).read_text()):
        sc = scenarios.get(row["scenario"])
        if sc is None:
            continue  # scenario since removed
        summary = summary_from_trace(ROOT / row["trace"])
        out.append(RunResult(sc, row["model"], row["guard"], row["run"], summary, score(sc, summary, row["effects"])))
    return out


# ---------------------------------------------------------------- report


def frac(k: int, n: int) -> str:
    return f"{k}/{n}" if n else "—"


def quote(s: str | None) -> str:
    if not s:
        return "> _(empty)_"
    return "\n".join("> " + line if line else ">" for line in s.strip().splitlines())


def expect_str(sc: Scenario) -> str:
    e = sc.expect
    return f"{e.effects} {e.table[:-1] if e.effects == 1 else e.table}" + (" (ask ok)" if e.ask_ok else "")


def agg(rs: list[RunResult]) -> dict:
    valid = [r for r in rs if r.score.status in ("pass", "fail")]
    return {
        "runs": len(rs),
        "valid": len(valid),
        "pass": sum(r.score.status == "pass" for r in valid),
        "clean": sum(r.score.clean_pass for r in valid),
        "interventions": sum(r.score.interventions for r in rs),
        "duplicates": sum(r.score.duplicates for r in valid),
        "blocked": sum(r.score.wrongly_blocked for r in valid),
        "asks": sum(r.score.asks for r in rs),
        "invalid": sum(r.score.status == "invalid" for r in rs),
        "error": sum(r.score.status == "error" for r in rs),
        "tokens": sum(r.summary.total_tokens for r in rs),
    }


def row(label: str, a: dict, extra: str = "", clean: bool = False) -> str:
    inv = str(a["invalid"]) + (f" (+{a['error']} error)" if a["error"] else "")
    cl = frac(a["clean"], a["valid"]) if clean else "—"
    return (f"| {label} |{extra} **{frac(a['pass'], a['valid'])}** | {cl} | {a['duplicates']} | {a['blocked']} "
            f"| {a['asks']} | {a['interventions']} | {inv} | {a['tokens']:,} |")


def token_rows(label: str, rs: list[RunResult]) -> str:
    u = [r.summary.usage for r in rs]
    j = [r.summary.judge_usage for r in rs]
    return (f"| {label} | {sum(x.input_tokens for x in u):,} | {sum(x.output_tokens for x in u):,} "
            f"| {sum(x.cache_creation_input_tokens for x in u):,} | {sum(x.cache_read_input_tokens for x in u):,} "
            f"| {sum(x.input_tokens + x.cache_creation_input_tokens + x.cache_read_input_tokens for x in j):,} "
            f"| {sum(x.output_tokens for x in j):,} | {sum(r.summary.total_tokens for r in rs):,} |")


TOKEN_HEAD = ["| | agent input (uncached) | agent output | cache write | cache read | judge input | judge output | total |",
              "|---|---|---|---|---|---|---|---|"]


def call_seq(r: RunResult) -> str:
    parts = []
    for c in r.summary.tool_calls:
        tag = []
        if c.forced:
            tag.append("forced")
        if c.timed_out:
            tag.append("timeout")
        if c.decision not in ("execute",):
            tag.append(c.decision)
        parts.append(c.name + (f" ({', '.join(tag)})" if tag else ""))
    return " → ".join(parts) or "—"


def report(results: list[RunResult], scenarios: list[Scenario], models: list[str], guard: str) -> str:
    by = lambda sc, m: [r for r in results if r.scenario.id == sc.id and r.model == m]  # noqa: E731
    total_tokens = sum(r.summary.total_tokens for r in results)
    runs_per = sorted({len(by(sc, m)) for sc in scenarios for m in models} - {0})
    md = [
        f"# DoubleTake Phase 2 — guard `{guard}`",
        "",
        f"- Generated: {datetime.now().isoformat(timespec='seconds')}",
        f"- Models: {', '.join(f'`{m}`' for m in models)}",
        f"- Scenarios: {len(scenarios)}; runs per scenario × model: {', '.join(map(str, runs_per)) or '—'}",
        f"- Total tokens: **{total_tokens:,}**",
        "",
        "Effects are rows counted in the database. *Pass* = exact expected count (for *ask ok* retry cases, "
        "also 1 effect + the agent asked). Pass rates are over valid runs: *invalid* runs (the lost reply "
        "never fired in a scenario that needs it) and *error* runs (API error) count as neither pass nor fail. "
        "*Duplicates* / *wrongly blocked* sum `max(0, actual−expected)` / `max(0, expected−actual)` over valid runs. "
        "*Asks* = model asks (reply matches an ask pattern) + guard asks. "
        "*Interventions* = non-Execute guard decisions (return_result / block / ask_user) on calls to the "
        "scenario's effect tool; they never change pass/fail. *Clean pass* (intentional cases) = pass with zero "
        "interventions; a pass with interventions means the model got past the guard (\"passed via workaround\").",
        "",
        "## Overview",
        "",
        "| model | retry pass | intentional pass | intentional clean pass | all pass | duplicates | wrongly blocked "
        "| asks | interventions | invalid | tokens |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for m in models:
        rs = [r for r in results if r.model == m]
        a = agg(rs)
        ar = agg([r for r in rs if r.scenario.category == "retry"])
        ai = agg([r for r in rs if r.scenario.category == "intentional"])
        inv = str(a["invalid"]) + (f" (+{a['error']} error)" if a["error"] else "")
        md.append(f"| `{m}` | {frac(ar['pass'], ar['valid'])} | {frac(ai['pass'], ai['valid'])} "
                  f"| {frac(ai['clean'], ai['valid'])} | **{frac(a['pass'], a['valid'])}** | {a['duplicates']} "
                  f"| {a['blocked']} | {a['asks']} | {a['interventions']} | {inv} | {a['tokens']:,} |")

    md += ["", "## Tokens", "",
           "Agent = the model under test; judge = guard-side LLM calls (always claude-haiku-5-5). "
           "Totals include both.", "", *TOKEN_HEAD]
    for m in models:
        md.append(token_rows(f"`{m}`", [r for r in results if r.model == m]))
    md.append(token_rows("**all**", results))

    for m in models:
        md += ["", f"## `{m}`", "",
               "| scenario | category | expect | pass | clean pass | duplicates | wrongly blocked | asks "
               "| interventions | invalid | tokens |",
               "|---|---|---|---|---|---|---|---|---|---|---|"]
        for sc in scenarios:
            md.append(row(sc.id, agg(by(sc, m)), f" {sc.category} | {expect_str(sc)} |",
                          clean=sc.category == "intentional"))
        for cat in ("retry", "intentional"):
            rs = [r for r in results if r.model == m and r.scenario.category == cat]
            md.append(row(f"**all {cat} cases**", agg(rs), " | |", clean=cat == "intentional"))

    fails = [r for r in results if r.score.status == "fail"]
    md += ["", f"## Failing runs ({len(fails)})", ""]
    if not fails:
        md.append("None.")
    else:
        md += ["| scenario | model | run | effects (expected) | asks | tool calls | trace |", "|---|---|---|---|---|---|---|"]
        for r in fails:
            md.append(f"| {r.scenario.id} | `{r.model}` | {r.n} | {r.score.effects} ({r.score.expected}) "
                      f"| {r.score.asks} | {call_seq(r)} | `{rel(r.summary.trace_path)}` |")

    wk = [r for r in results if r.score.workaround]
    md += ["", f"## Intentional runs that passed via workaround ({len(wk)})", "",
           "Passed, but the guard intervened on an effect call first and the model got past it.", ""]
    if not wk:
        md.append("None.")
    for r in wk:
        md.append(f"- {r.scenario.id} / `{r.model}` run {r.n}: {r.score.interventions} intervention(s) — "
                  f"{call_seq(r)} — `{rel(r.summary.trace_path)}`")

    odd = [r for r in results if r.score.status in ("invalid", "error")]
    md += ["", f"## Invalid and error runs ({len(odd)})", ""]
    if not odd:
        md.append("None.")
    for r in odd:
        why = "lost reply never fired" if r.score.status == "invalid" else next(
            (t.api_error for t in r.summary.turns if t.api_error), "API error")
        md.append(f"- **{r.score.status.upper()}** {r.scenario.id} / `{r.model}` run {r.n}: {why} — "
                  f"{call_seq(r)} — `{rel(r.summary.trace_path)}`")

    md += ["", "## All runs", "",
           "| scenario | model | run | status | effects | fault hit | asks (model+guard) | interventions "
           "| tool calls | tokens | trace |",
           "|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in results:
        s = r.score
        md.append(f"| {r.scenario.id} | `{r.model}` | {r.n} | {s.status} | {s.effects}/{s.expected} "
                  f"| {'yes' if s.fault_hit else 'no'} | {s.model_asks}+{s.guard_asks} | {s.interventions} | {call_seq(r)} "
                  f"| {r.summary.total_tokens:,} | `{rel(r.summary.trace_path)}` |")

    md += ["", "## Final replies", "",
           "The last assistant text after every user message (and after every forced tool call), verbatim.", ""]
    for r in results:
        md += [f"### {r.scenario.id} / `{r.model}` / run {r.n} — {r.score.status}, "
               f"{r.score.effects}/{r.score.expected} {r.scenario.expect.table}", ""]
        for i, t in enumerate(r.summary.turns, start=1):
            who = {"script": "user", "on_ask": "user (on_ask reply)", "forced": "forced tool call"}.get(t.source, t.source)
            md += [f"**{i}. {who}:** {t.user_text}", ""]
            if t.api_error:
                md += [f"> ⚠ API error: {t.api_error}", ""]
                continue
            hits = asks_user(t.reply)
            md += [quote(t.reply), ""]
            if hits:
                md += ["*Counted as asking:* " + "; ".join(f"“{h}”" for h in hits), ""]

    md += ["## Ask patterns", "", "A reply sentence counts as asking if it matches (case-insensitive):", ""]
    md += [f"- `{p}`" for p in ASK_PATTERNS]
    return "\n".join(md) + "\n"
