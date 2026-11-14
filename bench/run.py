"""DoubleTake test kitchen: run the scenario suite against a guard.

    python -m bench.run --guard none --models claude-haiku-5-5 --runs 1
    python -m bench.run --guard none --models claude-opus-5-5,claude-sonnet-5-5,claude-haiku-5-5 --runs 3
    python -m bench.run --guard none --models claude-haiku-5-5 --scenarios pending_lookup --runs 2 --merge

Writes results/phase2/<guard>_runs.json (one row per run) and results/phase2/<guard>.md.
With --merge, new runs replace saved ones with the same (scenario, model, run) and the
report is rebuilt from all of them (details come from the traces in logs/).
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from agent.guards import GUARDS
from agent.loop import DEFAULT_TENANT, ROOT
from bench.driver import RunResult, run_scenario
from bench.report import RESULTS_DIR, load_runs, rel, report, save_runs
from bench.scenario import load_scenarios


async def run_all(scenarios, models, guard, runs, concurrency, tenant_id, tool_timeout) -> list[RunResult]:
    sem = asyncio.Semaphore(concurrency)

    async def one(sc, m, n):
        async with sem:
            r = await run_scenario(sc, m, guard, n, tenant_id=tenant_id, tool_timeout=tool_timeout)
        s = r.score
        print(f"{sc.id:28} {m.removeprefix('claude-'):12} run {n}: {s.status.upper():7} "
              f"{s.effects}/{s.expected} {sc.expect.table} | asks {s.asks} | {r.summary.usage.total:,} tok",
              flush=True)
        return r

    return list(await asyncio.gather(*[one(sc, m, n) for sc in scenarios for m in models
                                       for n in range(1, runs + 1)]))


def main() -> int:
    load_dotenv(ROOT / ".env")
    ap = argparse.ArgumentParser(description="DoubleTake scenario benchmark")
    ap.add_argument("--guard", default="none", choices=sorted(GUARDS))
    ap.add_argument("--models", default=os.environ.get("ANTHROPIC_MODEL", "claude-opus-5-5"),
                    help="comma-separated model ids")
    ap.add_argument("--runs", type=int, default=1, help="runs per scenario × model")
    ap.add_argument("--scenarios", help="comma-separated scenario ids (default: all)")
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--tenant", default=DEFAULT_TENANT)
    ap.add_argument("--merge", action="store_true", help="merge with saved runs for this guard")
    ap.add_argument("--results-dir", default=str(RESULTS_DIR),
                    help="where <guard>.md and <guard>_runs.json go (default results/phase2)")
    args = ap.parse_args()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY is not set.", file=sys.stderr)
        return 1

    all_scenarios = load_scenarios()
    ids = [s.strip() for s in args.scenarios.split(",")] if args.scenarios else None
    scenarios = load_scenarios(ids=ids) if ids else all_scenarios
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    tool_timeout = float(os.environ.get("DOUBLETAKE_TOOL_TIMEOUT", 10))

    results = asyncio.run(run_all(scenarios, models, args.guard, args.runs, args.concurrency,
                                  args.tenant, tool_timeout))

    results_dir = Path(args.results_dir)
    if not results_dir.is_absolute():
        results_dir = ROOT / results_dir
    runs_path = results_dir / f"{args.guard}_runs.json"
    if args.merge and runs_path.exists():
        new = {(r.scenario.id, r.model, r.n) for r in results}
        prior = [r for r in load_runs(runs_path, {s.id: s for s in all_scenarios})
                 if (r.scenario.id, r.model, r.n) not in new]
        results = prior + results
        models = list(dict.fromkeys([r.model for r in prior] + models))
    order = [s.id for s in all_scenarios]
    results.sort(key=lambda r: (models.index(r.model), order.index(r.scenario.id), r.n))
    shown = [s for s in all_scenarios if any(r.scenario.id == s.id for r in results)]

    save_runs(results, runs_path)
    out = results_dir / f"{args.guard}.md"
    out.write_text(report(results, shown, models, args.guard))
    print(f"\ntotal tokens this invocation+merged: {sum(r.summary.usage.total for r in results):,}")
    print(f"written: {rel(out)}, {rel(runs_path)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
