"""Run every design and crash rate across several seeds and summarize.

Reports mean, min and max across seeds for:
  - duplicate side effects per 1,000 runs
  - percentage of runs with at least one duplicate
  - extra step executions (% over a crash-free run)

Usage:
  python seed_sweep.py                  # 100,000 runs per seed (~3 min)
  python seed_sweep.py --runs 10000     # faster
  python seed_sweep.py --json out.json  # also write raw per-seed results
"""
import argparse
import json
import statistics
from ckptsim import CRASH_RATES, DESIGNS, simulate

SEEDS = [1, 7, 42, 99, 2026]
METRICS = [
    ("duplicate_side_effects_per_1000_runs", "Dup/1,000 runs"),
    ("runs_with_any_duplicate_pct", "Runs with a duplicate (%)"),
    ("extra_step_executions_pct", "Extra step executions (%)"),
]


def fmt(vals):
    m = statistics.mean(vals)
    return f"{m:.1f} ({min(vals):.1f}-{max(vals):.1f})"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=100000)
    ap.add_argument("--json", help="write raw per-seed results to this file")
    args = ap.parse_args()

    raw = []
    for p in CRASH_RATES:
        for d in DESIGNS:
            for s in SEEDS:
                raw.append(simulate(d, p, args.runs, s))

    print(f"Mean (min-max) across seeds {SEEDS}, {args.runs:,} runs per seed\n")
    for key, label in METRICS:
        print(f"### {label}\n")
        print("| Design | 1% | 5% | 10% |")
        print("| --- | --- | --- | --- |")
        for d in DESIGNS:
            cells = []
            for p in CRASH_RATES:
                vals = [r[key] for r in raw if r["design"] == d and r["p_crash_per_step"] == p]
                cells.append(fmt(vals))
            print(f"| {d} | " + " | ".join(cells) + " |")
        print()

    if args.json:
        with open(args.json, "w") as f:
            json.dump(raw, f, indent=1)


if __name__ == "__main__":
    main()
