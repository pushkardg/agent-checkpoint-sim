# Agent memory prototype

A runnable prototype of the workflow-agent memory designs from *Designing Memory for LLM Agents: Why One Architecture Doesn't Fit Every Workload*. It complements the statistical simulator (`ckptsim.py`) with real moving parts:

- **A real model call per step.** Tool arguments come from Claude through the Anthropic Messages API (forced tool use), or from an offline mock model whose wording varies on every call, like sampling at temperature 1.
- **A real downstream service.** An HTTP service backed by SQLite applies refunds, emails and ticket notes. It supports Stripe-style idempotency keys: a repeated key with the same request returns the stored result, and a repeated key with a different request is rejected with HTTP 409.
- **Real crashes.** Each workflow runs in a separate worker process. A crash is `os._exit(137)`, so anything not committed to the checkpoint store is lost. The runner restarts the worker, which resumes from its checkpoint.
- **A durable checkpoint store.** SQLite with `synchronous=FULL` holds the ticket, completed step outputs and the intent record for every side-effecting operation. The working context for each model call is assembled from it.

This is a prototype for studying failure behavior, not a production framework.

## Workflow

`classify → refund → notify_customer → update_ticket → summarize → close`

Five steps call the model. Three steps (`refund`, `notify_customer`, `update_ticket`) have side effects on the downstream service.

## Memory designs

| Design | Idempotency key | On retry or resume |
| --- | --- | --- |
| `checkpoint_only` | none | re-plan the arguments with the model and call again |
| `idem_args` | hash of the tool arguments | re-plan; new arguments produce a new key |
| `idem_op_regen` | logical operation ID `run:step:seq` | re-plan; the same key with different arguments gets HTTP 409 and the run parks |
| `idem_op` | logical operation ID `run:step:seq` | replay the exact stored request with the same key |
| `status_check` | none (service has no key support) | write a durable intent record; on resume or after a failed call, ask the service for APPLIED / NOT_APPLIED / UNKNOWN; replay the stored request only on NOT_APPLIED, park on UNKNOWN |

## Fault model

- **Process crashes.** Each step execution crashes with probability `--crash-p` at one of four boundaries shared by every design: before the step, after the intent record is written (a no-op for designs without one), after the call returns but before the checkpoint, and after the checkpoint. Crashes are sampled deterministically from (seed, run, step, attempt), so all designs face the same schedule.
- **Lost responses.** With probability `--lost-response-p`, the service applies and commits the effect, then returns HTTP 502, as if the reply were lost.
- **Status lag.** `--status-lag` seconds pass before the status endpoint reports an applied effect, modeling eventual consistency and in-flight work.
- **Status outages.** With probability `--status-outage-p`, the status endpoint returns 503, which the agent treats as UNKNOWN.

Duplicates are counted from the service's own effect log.

## Running it

Python 3.9 or later. The mock model needs only the standard library.

```bash
# Offline, mock model (about 2 minutes for 200 runs per design)
python -m proto.runner --runs 200 --crash-p 0.05 --lost-response-p 0.05

# Add a lagging, sometimes-unavailable status endpoint
python -m proto.runner --runs 200 --crash-p 0.05 --lost-response-p 0.05 \
    --status-lag 0.5 --status-outage-p 0.05 --designs idem_op status_check

# Real Claude calls
pip install -r requirements.txt
export ANTHROPIC_API_KEY=...
export ANTHROPIC_MODEL=claude-haiku-5-5     # optional; this is the default
python -m proto.runner --runs 30 --model claude --json results/baseline_claude.json
```

With `--model claude`, each run makes about five API calls, so 30 runs across five designs is roughly 750 calls. Token counts are recorded in the usage log in the work directory.

## Results (mock model, seed 42, 200 runs per design)

**Baseline:** `--crash-p 0.05 --lost-response-p 0.05`

| Design | Duplicate effects | Runs with a duplicate | 409 conflicts | Parked runs | Completed | Model calls / run |
| --- | --- | --- | --- | --- | --- | --- |
| checkpoint_only | 46 | 36 | 0 | 0 | 200 | 5.29 |
| idem_args | 35 | 30 | 0 | 0 | 200 | 5.29 |
| idem_op_regen | 0 | 0 | 26 | 26 | 174 | 4.96 |
| idem_op | 0 | 0 | 0 | 0 | 200 | 5.02 |
| status_check | 0 | 0 | 0 | 0 | 200 | 5.02 |

**Lagging and unreliable status endpoint:** add `--status-lag 0.5 --status-outage-p 0.05`

| Design | Duplicate effects | Runs with a duplicate | 409 conflicts | Parked runs | Completed | Model calls / run |
| --- | --- | --- | --- | --- | --- | --- |
| idem_op | 0 | 0 | 0 | 0 | 200 | 5.02 |
| status_check | 36 | 29 | 0 | 1 | 199 | 5.01 |

Raw results are in `results/`.

## What the prototype shows

1. **Argument-derived keys fail with LLM agents.** When a step is re-planned after a crash or lost response, the model produces different wording, so the hash changes and the service applies the effect again. `idem_args` cut duplicates only slightly compared with no key at all.
2. **A stable key with regenerated arguments trades duplicates for stuck runs.** The service correctly rejects the changed request with 409, and the run parks for manual reconciliation.
3. **A stable key plus the stored request is the robust pattern.** `idem_op` produced no duplicates and no parked runs, and it saves model calls because retries replay the stored request instead of re-planning.
4. **Status checks are only as good as the status endpoint.** With immediate, authoritative status, `status_check` matched `idem_op`. With a lagging endpoint, a lookup right after a lost response reported NOT_APPLIED for an effect that had already landed, and the replay created a duplicate.

## Limitations

- Results above use the offline mock model. With Claude, the size of the `idem_args` effect depends on how much the model's wording varies between calls.
- One logical operation per step, so the operation sequence number is always 0.
- Single service, single worker at a time; no concurrent workers or lease expiry.
- Crash boundaries are uniform; real failures cluster around slow calls and timeouts.

## Layout

```
proto/service.py     downstream HTTP service, SQLite effect log, fault injection
proto/checkpoint.py  durable checkpoint and intent-record store
proto/model.py       Claude and mock model backends
proto/agent.py       worker process: workflow, memory designs, crash injection
proto/runner.py      experiment harness and results table
```
