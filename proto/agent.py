"""Agent worker: runs one support workflow until it completes, parks or crashes.

Run as a separate process. A crash is a real `os._exit(137)`: nothing that
was not committed to the checkpoint store survives. The runner restarts the
worker, which resumes from its checkpoint.

Each restart is one "attempt". Crashes are sampled deterministically from
(crash seed, run, step, attempt), so every memory design faces the same
crash schedule. There are four crash boundaries shared by all designs:

  before_step                  before the step does anything
  after_started_before_call    after the intent record is written (a no-op
                               boundary for designs that write none)
  after_call_before_ckpt       after the tool call, before the checkpoint
  after_ckpt                   after the step checkpoint is committed

Memory designs for side-effecting steps:

  checkpoint_only    checkpoint after each step; no intent record, no key
  idem_args          key = hash(tool arguments); args regenerated on retry
  idem_op            key = run:step:seq; request persisted, replayed on retry
  idem_op_regen      key = run:step:seq, but args regenerated on retry
  status_check       request persisted; on resume, ask the service whether
                     the effect landed (APPLIED / NOT_APPLIED / UNKNOWN)
"""
import argparse
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request

from .checkpoint import CheckpointStore
from .model import make_model

STEPS = ["classify", "refund", "notify_customer", "update_ticket", "summarize", "close"]
SIDE_EFFECT = {"refund": "refund", "notify_customer": "email", "update_ticket": "ticket_note"}
BOUNDARIES = ["before_step", "after_started_before_call", "after_call_before_ckpt", "after_ckpt"]
MAX_RETRIES = 3

EXIT_DONE, EXIT_PARKED, EXIT_CRASH = 0, 10, 137


class Parked(Exception):
    pass


def _unit(*parts):
    h = hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()
    return int(h[:12], 16) / float(16 ** 12)


def http(method, url, body=None, headers=None, timeout=10):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")
    except (urllib.error.URLError, TimeoutError):
        return 599, {"error": "network"}


class Worker:
    def __init__(self, args):
        self.a = args
        self.store = CheckpointStore(args.ckpt_db)
        self.model = make_model(args.model, usage_log=args.usage_log, mock_latency_s=args.mock_latency)
        self.run_id = args.run_id
        self.status_probe = 0

    # crash injection
    def crash_point(self, step):
        u = _unit(self.a.crash_seed, self.run_id, step, self.a.attempt)
        if u >= self.a.crash_p:
            return None
        return BOUNDARIES[int(_unit(self.a.crash_seed, "where", self.run_id, step, self.a.attempt) * 4)]

    @staticmethod
    def maybe_crash(point, here):
        if point == here:
            sys.stdout.flush()
            os._exit(EXIT_CRASH)

    # downstream calls
    def call(self, step, payload, key=None):
        headers = {"Idempotency-Key": key} if key else {}
        body = {"run_id": self.run_id, "step": step, "kind": SIDE_EFFECT[step], "payload": payload}
        return http("POST", f"{self.a.service}/effects", body, headers)

    def status(self, step):
        self.status_probe += 1
        code, out = http("GET", f"{self.a.service}/status?run_id={self.run_id}&step={step}"
                                f"&probe={self.a.attempt}-{self.status_probe}")
        if code != 200:
            return "UNKNOWN"
        return out["status"]

    # one side-effecting step under the chosen design
    def side_effect(self, step, context, point):
        d = self.a.design
        op_id = f"{self.run_id}:{step}:0"
        record = self.store.tool_call(op_id)

        if d in ("status_check", "idem_op_regen") and record and record["status"] == "done":
            return record["result"]                          # completed before the crash
        if d == "status_check" and record and record["status"] == "started":
            st = self.status(step)
            if st == "APPLIED":
                return {"skipped": "already applied"}
            if st == "UNKNOWN":
                raise Parked(f"{step}: status unknown on resume")
            payload = record["request"]                      # NOT_APPLIED: replay stored request
        elif d == "idem_op" and record:
            payload = record["request"]                      # replay the exact stored request
        else:
            payload = self.model.plan(step, context)         # (re)generate arguments

        if d in ("idem_op", "idem_op_regen", "status_check") and not (record and d == "idem_op"):
            self.store.record_started(op_id, self.run_id, step, payload)
        self.maybe_crash(point, "after_started_before_call")

        for attempt in range(MAX_RETRIES + 1):
            if d == "idem_args":
                key = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:24]
            elif d in ("idem_op", "idem_op_regen"):
                key = op_id
            else:
                key = None
            code, out = self.call(step, payload, key)
            if code == 200:
                if d in ("idem_op", "idem_op_regen", "status_check"):
                    self.store.record_done(op_id, out)
                return out
            if code == 409:
                raise Parked(f"{step}: idempotency key reused with different arguments")
            # 502 / network: outcome unknown to the agent
            if d == "status_check":
                st = self.status(step)
                if st == "APPLIED":
                    self.store.record_done(op_id, {"recovered": True})
                    return {"recovered": True}
                if st == "UNKNOWN":
                    raise Parked(f"{step}: status unknown after failed call")
                continue                                     # NOT_APPLIED: retry stored request
            if d in ("checkpoint_only", "idem_args", "idem_op_regen"):
                payload = self.model.plan(step, context)     # frameworks re-run the step
        raise Parked(f"{step}: retries exhausted")

    def run(self):
        run = self.store.run(self.run_id)
        if run["status"] != "running":
            return EXIT_DONE if run["status"] == "done" else EXIT_PARKED
        done = self.store.completed_steps(self.run_id)
        try:
            for step in STEPS:
                if step in done:
                    continue
                point = self.crash_point(step)
                self.maybe_crash(point, "before_step")
                # Working context is assembled from the checkpoint on every step.
                context = {"ticket": run["ticket"], "completed_steps": done}
                if step in SIDE_EFFECT:
                    output = self.side_effect(step, context, point)
                elif step == "close":
                    self.maybe_crash(point, "after_started_before_call")
                    output = {"closed": True}
                else:
                    self.maybe_crash(point, "after_started_before_call")
                    output = self.model.plan(step, context)
                self.maybe_crash(point, "after_call_before_ckpt")
                self.store.complete_step(self.run_id, step, output)
                done[step] = output
                self.maybe_crash(point, "after_ckpt")
        except Parked as e:
            self.store.set_run_status(self.run_id, "parked", str(e))
            return EXIT_PARKED
        self.store.set_run_status(self.run_id, "done")
        return EXIT_DONE


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--run-id", required=True)
    p.add_argument("--design", required=True,
                   choices=["checkpoint_only", "idem_args", "idem_op", "idem_op_regen", "status_check"])
    p.add_argument("--ckpt-db", required=True)
    p.add_argument("--service", required=True)
    p.add_argument("--model", default="mock", choices=["mock", "claude"])
    p.add_argument("--mock-latency", type=float, default=0.0)
    p.add_argument("--usage-log")
    p.add_argument("--crash-p", type=float, default=0.0)
    p.add_argument("--crash-seed", type=int, default=0)
    p.add_argument("--attempt", type=int, default=0)
    args = p.parse_args(argv)
    sys.exit(Worker(args).run())


if __name__ == "__main__":
    main()
