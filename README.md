# agent-checkpoint-sim

Fault-injection simulation of memory designs for LLM workflow agents. It measures how often a crash causes a side effect (a refund, an email, a ticket update) to be applied twice.

This is a simulation, not a production benchmark. It accompanies the article *Designing Memory for LLM Agents: Why One Architecture Doesn't Fit Every Workload*.

## What it simulates

A six-step support workflow: `classify → refund → notify_customer → update_ticket → summarize → close`. Three steps call an external service with side effects.

At each step the process may crash, with probability *p*, at one of four points chosen uniformly:

1. before the step starts
2. after the "started" marker is written, before the tool call (designs without a marker crash at the same point, just before the call)
3. after the tool call returns, before the checkpoint is written
4. after the checkpoint is written

Four recovery designs are compared:

| Design | Behavior |
| --- | --- |
| `no_persistence` | State in memory only; a crash restarts from step one |
| `checkpoint_only` | State checkpointed after each step |
| `idempotency_key` | Checkpoint plus a per-call idempotency key from a stable logical operation ID (`run_id:step:op_seq`, not model-generated arguments), deduplicated downstream |
| `started_record_status_check` | Durable "started" marker before the call; on resume, query whether the effect landed |

The external service records every side effect it applies, so duplicates are counted from the service's point of view.

## Assumptions

- Each step execution, including re-executions after a restart, crashes independently with probability *p* (1%, 5%, 10%).
- A crash lands with equal probability at one of the four points above.
- A crashed run restarts immediately. Restarts are capped at 50 per run; no run reaches the cap, so completion is 100%.
- The downstream service is strongly consistent. For `idempotency_key`, it keeps every deduplication record for the whole experiment. For `started_record_status_check`, the status query is always available and authoritative: it returns `APPLIED` or `NOT_APPLIED`, never `UNKNOWN`.
- Ambiguous or unavailable status checks are not modeled. The code has an `UNKNOWN` branch that parks the step for reconciliation (raises `NeedsReconciliation`), but the simulated service never reaches it. A production service can.
- An "extra step execution" is any step run beyond the six a crash-free run needs.
- The "started" marker (with the persisted request payload) and the step checkpoint are durable writes that survive a crash; in-memory state does not survive in `no_persistence`.
- Lost responses (the tool succeeds but its reply never arrives) and eventually consistent or expiring downstream records are not modeled.

## Requirements

Python 3.9 or later. Standard library only.

## Usage

```bash
git clone https://github.com/pushkardg/agent-checkpoint-sim.git
cd agent-checkpoint-sim
python ckptsim.py --table                       # markdown summary, 10,000 runs
python ckptsim.py --runs 100000 --table         # the article's numbers
python ckptsim.py --runs 100000 > results.json  # full JSON results
python seed_sweep.py --json results_seeds.json  # all metrics, 5 seeds, mean (min-max), ~3 min
```

## Results

From `python seed_sweep.py --json results_seeds.json`. Mean (min-max) across seeds [1, 7, 42, 99, 2026], 100,000 runs per seed.

#### Dup/1,000 runs

| Design | 1% | 5% | 10% |
| --- | --- | --- | --- |
| no_persistence | 108.3 (106.5-110.0) | 592.9 (584.5-600.4) | 1360.2 (1352.8-1369.7) |
| checkpoint_only | 7.5 (7.1-7.8) | 38.9 (37.6-39.4) | 81.5 (81.0-82.4) |
| idempotency_key | 0.0 (0.0-0.0) | 0.0 (0.0-0.0) | 0.0 (0.0-0.0) |
| started_record_status_check | 0.0 (0.0-0.0) | 0.0 (0.0-0.0) | 0.0 (0.0-0.0) |

#### Runs with a duplicate (%)

| Design | 1% | 5% | 10% |
| --- | --- | --- | --- |
| no_persistence | 4.4 (4.4-4.5) | 20.6 (20.5-20.8) | 37.9 (37.7-38.0) |
| checkpoint_only | 0.7 (0.7-0.8) | 3.8 (3.6-3.9) | 7.7 (7.7-7.8) |
| idempotency_key | 0.0 (0.0-0.0) | 0.0 (0.0-0.0) | 0.0 (0.0-0.0) |
| started_record_status_check | 0.0 (0.0-0.0) | 0.0 (0.0-0.0) | 0.0 (0.0-0.0) |

#### Extra step executions (%)

| Design | 1% | 5% | 10% |
| --- | --- | --- | --- |
| no_persistence | 3.6 (3.6-3.7) | 20.1 (19.9-20.3) | 47.0 (46.8-47.2) |
| checkpoint_only | 0.7 (0.7-0.8) | 3.9 (3.9-3.9) | 8.1 (8.1-8.1) |
| idempotency_key | 0.7 (0.7-0.8) | 3.9 (3.9-3.9) | 8.1 (8.1-8.1) |
| started_record_status_check | 0.7 (0.7-0.8) | 3.9 (3.9-3.9) | 8.1 (8.1-8.1) |

#### Status-check branch counts (seed 42, 100,000 runs)

| Crash rate | APPLIED (skip) | NOT_APPLIED (replay) |
| --- | --- | --- |
| 1% | 765 | 703 |
| 5% | 3,939 | 3,707 |
| 10% | 8,109 | 7,912 |

The `NOT_APPLIED` branch runs whenever a crash lands after the started marker but before the call; the step is then replayed from the persisted request.

## Limitations

- Crash points are uniform. Real agents spend most of their time inside tool calls, which widens the window between "tool returned" and "checkpoint written".
- Timeouts that lose a response after the tool succeeded are not modeled separately. They fall in the same window, so `checkpoint_only` numbers are likely an underestimate.
- Zero duplicates for the recovery-aware designs mostly confirms the logic rather than tests it: the service keeps idempotency keys forever and never returns `UNKNOWN`. The finding is the size of the `checkpoint_only` gap and the low cost of closing it.

## License

MIT
