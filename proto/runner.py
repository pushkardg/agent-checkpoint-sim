"""Experiment runner.

Starts the downstream service, then for each memory design runs N support
workflows. Each workflow is a real worker process that is restarted after
every crash until it completes or parks. Results come from the service's own
effect log, the checkpoint store and the model usage log.

  python -m proto.runner --runs 200                       # offline mock model
  python -m proto.runner --runs 30 --model claude         # real Claude calls
"""
import argparse
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time

from .agent import EXIT_CRASH, EXIT_DONE, EXIT_PARKED
from .checkpoint import CheckpointStore
from .service import ServiceState, start_server

DESIGNS = ["checkpoint_only", "idem_args", "idem_op_regen", "idem_op", "status_check"]


def make_ticket(i):
    return {"ticket_id": f"T-{1000 + i}", "order_id": f"ORD-{50000 + i}", "customer": f"Customer {i}",
            "amount_usd": round(12 + (i * 7.31) % 180, 2),
            "message": "My order arrived damaged and I would like a refund."}


def run_design(design, args, workdir, base_url, svc):
    svc.reset()
    ckpt_db = os.path.join(workdir, f"ckpt_{design}.sqlite")
    usage = os.path.join(workdir, f"usage_{design}.jsonl")
    for f in (ckpt_db, usage):
        if os.path.exists(f):
            os.remove(f)
    store = CheckpointStore(ckpt_db)
    restarts, wall = 0, []
    for i in range(args.runs):
        run_id = f"r{i}"
        store.create_run(run_id, make_ticket(i))
        t0 = time.time()
        for attempt in range(args.max_restarts + 1):
            cmd = [sys.executable, "-m", "proto.agent", "--run-id", run_id, "--design", design,
                   "--ckpt-db", ckpt_db, "--service", base_url, "--model", args.model,
                   "--mock-latency", str(args.mock_latency), "--usage-log", usage,
                   "--crash-p", str(args.crash_p), "--crash-seed", str(args.seed), "--attempt", str(attempt)]
            rc = subprocess.run(cmd, cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))).returncode
            if rc in (EXIT_DONE, EXIT_PARKED):
                break
            if rc != EXIT_CRASH:
                raise RuntimeError(f"worker failed with exit code {rc} on {design} {run_id}")
            restarts += 1
        wall.append(time.time() - t0)

    stats = svc.stats()
    by_status = store.runs_by_status()
    calls = []
    if os.path.exists(usage):
        calls = [json.loads(line) for line in open(usage)]
    return {
        "design": design,
        "runs": args.runs,
        "completed": by_status.get("done", 0),
        "parked": by_status.get("parked", 0),
        "duplicate_effects": stats["duplicates"],
        "runs_with_duplicate": stats["runs_with_duplicate"],
        "conflicts_409": stats["conflicts_409"],
        "restarts": restarts,
        "model_calls": len(calls),
        "model_calls_per_run": round(len(calls) / args.runs, 2),
        "input_tokens": sum(c["input_tokens"] for c in calls),
        "output_tokens": sum(c["output_tokens"] for c in calls),
        "median_run_seconds": round(statistics.median(wall), 3),
    }


def to_table(results):
    cols = [("design", "Design"), ("duplicate_effects", "Duplicate effects"),
            ("runs_with_duplicate", "Runs with a duplicate"), ("conflicts_409", "409 conflicts"),
            ("parked", "Parked runs"), ("completed", "Completed"), ("model_calls_per_run", "Model calls / run"),
            ("median_run_seconds", "Median run (s)")]
    lines = ["| " + " | ".join(h for _, h in cols) + " |", "|" + " --- |" * len(cols)]
    for r in results:
        lines.append("| " + " | ".join(str(r[k]) for k, _ in cols) + " |")
    return "\n".join(lines)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--runs", type=int, default=100)
    p.add_argument("--designs", nargs="*", default=DESIGNS)
    p.add_argument("--model", default="mock", choices=["mock", "claude"])
    p.add_argument("--mock-latency", type=float, default=0.0, help="seconds of fake model latency per call")
    p.add_argument("--crash-p", type=float, default=0.05, help="crash probability per step execution")
    p.add_argument("--lost-response-p", type=float, default=0.05, help="P(effect applied but reply lost)")
    p.add_argument("--status-lag", type=float, default=0.0, help="seconds before status shows APPLIED")
    p.add_argument("--status-outage-p", type=float, default=0.0, help="P(status endpoint returns 503)")
    p.add_argument("--max-restarts", type=int, default=50)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--workdir", default=None)
    p.add_argument("--json", help="write results to this file")
    args = p.parse_args()

    if args.model == "claude" and not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("Set ANTHROPIC_API_KEY to use --model claude.")

    workdir = args.workdir or tempfile.mkdtemp(prefix="agentmem_")
    os.makedirs(workdir, exist_ok=True)
    svc = ServiceState(os.path.join(workdir, "service.sqlite"), seed=args.seed,
                       lost_response_p=args.lost_response_p, status_lag=args.status_lag,
                       status_outage_p=args.status_outage_p)
    server, base_url = start_server(svc)
    print(f"service at {base_url}; workdir {workdir}; model={args.model}; crash_p={args.crash_p}; "
          f"lost_response_p={args.lost_response_p}; status_lag={args.status_lag}s; "
          f"status_outage_p={args.status_outage_p}; runs={args.runs}", flush=True)

    results = []
    for d in args.designs:
        t0 = time.time()
        r = run_design(d, args, workdir, base_url, svc)
        results.append(r)
        print(f"  {d:<16} done in {time.time() - t0:.1f}s", flush=True)
    server.shutdown()

    print()
    print(to_table(results))
    if args.json:
        with open(args.json, "w") as f:
            json.dump({"config": vars(args), "results": results}, f, indent=1)


if __name__ == "__main__":
    main()
