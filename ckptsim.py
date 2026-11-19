"""agent-checkpoint-sim: fault-injection simulation of workflow-agent memory designs.

A workflow has six steps; three call an external tool with side effects
(refund, notify customer, update ticket). At each step the process may crash,
with probability p, at one of four points chosen uniformly:

  1. before the step does anything
  2. after the "started" marker is written, before the tool call
     (designs without a marker crash at the same point, just before the call)
  3. after the tool call returns, before the checkpoint is written
  4. after the checkpoint is written

After a crash the run restarts and recovers using one of four designs:

  no_persistence               state in memory only; restart from step one
  checkpoint_only              state checkpointed after each step
  idempotency_key              checkpoint + per-call key, deduped downstream
  started_record_status_check  durable "started" marker before the call;
                               on resume, query whether the effect landed

The external service logs every side effect it applies, so duplicates are
counted from the service's point of view.

Usage:
  python ckptsim.py                         # JSON results, defaults
  python ckptsim.py --runs 10000 --seed 42  # explicit settings
  python ckptsim.py --table                 # markdown table
"""
import argparse
import json
import random
import statistics
from collections import Counter

STEPS = ["classify", "refund", "notify_customer", "update_ticket", "summarize", "close"]
SIDE_EFFECT = {"refund", "notify_customer", "update_ticket"}
WINDOWS = ("before_step", "after_started_before_call", "after_call_before_ckpt", "after_ckpt")
DESIGNS = ["no_persistence", "checkpoint_only", "idempotency_key", "started_record_status_check"]
CRASH_RATES = (0.01, 0.05, 0.10)


APPLIED, NOT_APPLIED, UNKNOWN = "APPLIED", "NOT_APPLIED", "UNKNOWN"


class Crash(Exception):
    """Simulated process crash."""


class NeedsReconciliation(Exception):
    """Status of a side effect is unknown; park the step for reconciliation."""


class ExternalService:
    """Downstream API. Records every applied side effect.

    If honors_keys is True it deduplicates calls by idempotency key.
    effect_status() models an authoritative status-query endpoint.
    """

    def __init__(self, honors_keys: bool):
        self.honors_keys = honors_keys
        self.effects = []
        self.seen_keys = set()

    def call(self, run_id, step, key=None):
        if self.honors_keys and key is not None:
            if key in self.seen_keys:
                return "dedup"
            self.seen_keys.add(key)
        self.effects.append((run_id, step))
        return "ok"

    def effect_status(self, run_id, step):
        """Status lookup. Returns APPLIED, NOT_APPLIED or UNKNOWN.

        The simulated service is strongly consistent and always available,
        so it never returns UNKNOWN. A production service can.
        """
        return APPLIED if (run_id, step) in self.effects else NOT_APPLIED


def run_workflow(run_id, design, svc, p_crash, rng, stats, max_restarts=50):
    """Run one workflow to completion. Returns (completed, step_executions)."""
    # Durable checkpoint store. Survives crashes except in no_persistence.
    ckpt = {"completed": [], "tool_calls": {}}
    executions = 0
    for _ in range(max_restarts + 1):
        if design == "no_persistence":
            ckpt = {"completed": [], "tool_calls": {}}
        try:
            for step in STEPS:
                if step in ckpt["completed"]:
                    continue
                executions += 1
                crash_at = rng.choice(WINDOWS) if rng.random() < p_crash else None

                if crash_at == "before_step":
                    raise Crash()

                if step in SIDE_EFFECT:
                    key = f"{run_id}:{step}:0"          # logical op ID: run + step + op sequence (one op per step here)
                    request = {"run": run_id, "step": step}  # persisted, replayed as-is
                    if design in ("no_persistence", "checkpoint_only"):
                        if crash_at == "after_started_before_call":
                            raise Crash()
                        svc.call(run_id, step)
                    elif design == "idempotency_key":
                        ckpt["tool_calls"].setdefault(key, {"status": "started", "request": request})
                        if crash_at == "after_started_before_call":
                            raise Crash()
                        svc.call(run_id, step, key=key)
                    elif design == "started_record_status_check":
                        if key in ckpt["tool_calls"]:
                            status = svc.effect_status(run_id, step)
                            stats[status] = stats.get(status, 0) + 1
                            if status == APPLIED:
                                pass  # effect landed before the crash: skip
                            elif status == NOT_APPLIED:
                                if crash_at == "after_started_before_call":
                                    raise Crash()
                                svc.call(run_id, step)  # replay the persisted request
                            else:
                                # UNKNOWN: never retry blindly. Not reachable
                                # here because the service is always consistent.
                                raise NeedsReconciliation(key)
                        else:
                            ckpt["tool_calls"][key] = {"status": "started", "request": request}
                            if crash_at == "after_started_before_call":
                                raise Crash()
                            svc.call(run_id, step)
                elif crash_at == "after_started_before_call":
                    raise Crash()  # same point for steps without side effects

                if crash_at == "after_call_before_ckpt":
                    raise Crash()

                ckpt["completed"].append(step)  # the step checkpoint

                if crash_at == "after_ckpt":
                    raise Crash()
            return True, executions
        except Crash:
            continue
    return False, executions


def simulate(design, p_crash, runs, seed):
    rng = random.Random(seed)
    svc = ExternalService(honors_keys=(design == "idempotency_key"))
    completed, executions, stats = 0, [], {}
    for i in range(runs):
        ok, ex = run_workflow(f"r{i}", design, svc, p_crash, rng, stats)
        completed += ok
        executions.append(ex)

    counts = Counter(svc.effects)
    duplicates = sum(n - 1 for n in counts.values())
    dup_runs = {run for (run, _), n in counts.items() if n > 1}
    mean_ex = statistics.mean(executions)
    return {
        "design": design,
        "p_crash_per_step": p_crash,
        "runs": runs,
        "seed": seed,
        "completion_rate": completed / runs,
        "duplicate_side_effects_per_1000_runs": round(1000 * duplicates / runs, 1),
        "runs_with_any_duplicate_pct": round(100 * len(dup_runs) / runs, 2),
        "mean_step_executions": round(mean_ex, 2),
        "extra_step_executions_pct": round(100 * (mean_ex - len(STEPS)) / len(STEPS), 1),
        "status_checks": stats,
    }


def to_table(results):
    by = {(r["design"], r["p_crash_per_step"]): r for r in results}
    rates = " / ".join(f"{p:.0%}" for p in CRASH_RATES)
    lines = [
        f"| Design | Dup/1,000 runs ({rates}) | Runs with a duplicate ({rates}) | Extra step executions ({rates}) |",
        "| --- | --- | --- | --- |",
    ]
    for d in DESIGNS:
        cells = []
        for key, unit in (("duplicate_side_effects_per_1000_runs", ""),
                          ("runs_with_any_duplicate_pct", "%"),
                          ("extra_step_executions_pct", "%")):
            cells.append(" / ".join(f"{by[(d, p)][key]:g}{unit}" for p in CRASH_RATES))
        lines.append(f"| {d} | " + " | ".join(cells) + " |")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--runs", type=int, default=10000)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--table", action="store_true", help="print a markdown table")
    args = ap.parse_args()

    results = [simulate(d, p, args.runs, args.seed) for p in CRASH_RATES for d in DESIGNS]
    print(to_table(results) if args.table else json.dumps(results, indent=1))


if __name__ == "__main__":
    main()
