"""Downstream service with real side effects, served over HTTP.

Stands in for a payments / email / ticketing API. Every applied effect is
committed to SQLite, so duplicates are counted from the service's own log.

Fault injection (deterministic, keyed by run, step and call number, so every
memory design sees the same faults):
  - lost response: the effect is applied and committed, then the client gets
    HTTP 502, as if the reply was lost in the network
  - status lag: GET /status only reports an effect as APPLIED after
    `status_lag` seconds (eventual consistency / in-flight work)
  - status outage: GET /status returns 503 (the caller sees UNKNOWN)

Idempotency follows the Stripe convention: a repeated key with the same
request returns the stored result; a repeated key with a different request
is rejected with HTTP 409.
"""
import hashlib
import json
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


def _unit(*parts):
    """Deterministic number in [0, 1) from the given parts."""
    h = hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()
    return int(h[:12], 16) / float(16 ** 12)


class ServiceState:
    def __init__(self, db_path, seed=0, lost_response_p=0.0, status_lag=0.0, status_outage_p=0.0):
        self.seed = seed
        self.lost_response_p = lost_response_p
        self.status_lag = status_lag
        self.status_outage_p = status_outage_p
        self.lock = threading.Lock()
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.db.executescript(
            """
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS effects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT, step TEXT, kind TEXT, payload TEXT, applied_at REAL);
            CREATE TABLE IF NOT EXISTS idem (
                key TEXT PRIMARY KEY, fingerprint TEXT, effect_id INTEGER);
            CREATE TABLE IF NOT EXISTS calls (
                run_id TEXT, step TEXT, n INTEGER, PRIMARY KEY (run_id, step));
            CREATE TABLE IF NOT EXISTS conflicts (
                key TEXT, run_id TEXT, step TEXT, at REAL);
            """
        )
        self.db.commit()

    def reset(self):
        with self.lock:
            self.db.executescript("DELETE FROM effects; DELETE FROM idem; DELETE FROM calls; DELETE FROM conflicts;")
            self.db.commit()

    def _next_call_number(self, run_id, step):
        row = self.db.execute("SELECT n FROM calls WHERE run_id=? AND step=?", (run_id, step)).fetchone()
        n = (row[0] + 1) if row else 1
        self.db.execute("INSERT OR REPLACE INTO calls VALUES (?,?,?)", (run_id, step, n))
        return n

    def apply(self, run_id, step, kind, payload, key):
        """Returns (http_status, body)."""
        fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        with self.lock:
            n = self._next_call_number(run_id, step)
            if key is not None:
                row = self.db.execute("SELECT fingerprint, effect_id FROM idem WHERE key=?", (key,)).fetchone()
                if row:
                    if row[0] != fingerprint:
                        self.db.execute("INSERT INTO conflicts VALUES (?,?,?,?)", (key, run_id, step, time.time()))
                        self.db.commit()
                        return 409, {"error": "idempotency key reused with different parameters"}
                    self.db.commit()
                    return 200, {"effect_id": row[1], "deduplicated": True}
            cur = self.db.execute(
                "INSERT INTO effects (run_id, step, kind, payload, applied_at) VALUES (?,?,?,?,?)",
                (run_id, step, kind, json.dumps(payload), time.time()),
            )
            effect_id = cur.lastrowid
            if key is not None:
                self.db.execute("INSERT INTO idem VALUES (?,?,?)", (key, fingerprint, effect_id))
            self.db.commit()
        if _unit(self.seed, "lost", run_id, step, n) < self.lost_response_p:
            return 502, {"error": "upstream timeout"}  # applied, but the reply is lost
        return 200, {"effect_id": effect_id, "deduplicated": False}

    def status(self, run_id, step, probe):
        if _unit(self.seed, "outage", run_id, step, probe) < self.status_outage_p:
            return 503, {"error": "status service unavailable"}
        with self.lock:
            row = self.db.execute(
                "SELECT MIN(applied_at) FROM effects WHERE run_id=? AND step=?", (run_id, step)
            ).fetchone()
        applied_at = row[0]
        if applied_at is not None and time.time() - applied_at >= self.status_lag:
            return 200, {"status": "APPLIED"}
        return 200, {"status": "NOT_APPLIED"}

    def stats(self):
        with self.lock:
            per_step = self.db.execute(
                "SELECT run_id, step, COUNT(*) FROM effects GROUP BY run_id, step"
            ).fetchall()
            conflicts = self.db.execute("SELECT COUNT(*) FROM conflicts").fetchone()[0]
        duplicates = sum(c - 1 for _, _, c in per_step)
        runs_with_dup = len({r for r, _, c in per_step if c > 1})
        return {"effects": sum(c for _, _, c in per_step), "duplicates": duplicates,
                "runs_with_duplicate": runs_with_dup, "conflicts_409": conflicts}


def make_handler(state):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, code, body):
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            url = urlparse(self.path)
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            if url.path == "/effects":
                code, out = state.apply(body["run_id"], body["step"], body["kind"], body["payload"],
                                        self.headers.get("Idempotency-Key"))
                self._send(code, out)
            elif url.path == "/reset":
                state.reset()
                self._send(200, {"ok": True})
            else:
                self._send(404, {"error": "not found"})

        def do_GET(self):
            url = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            if url.path == "/status":
                code, out = state.status(q["run_id"], q["step"], q.get("probe", "0"))
                self._send(code, out)
            elif url.path == "/stats":
                self._send(200, state.stats())
            else:
                self._send(404, {"error": "not found"})

    return Handler


def start_server(state, port=0):
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"
