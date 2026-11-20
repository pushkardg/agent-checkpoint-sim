"""Durable checkpoint store (SQLite, synchronous=FULL).

This is the agent's persistent session memory: the ticket, completed step
outputs, and the intent record for every side-effecting operation.
"""
import json
import sqlite3


class CheckpointStore:
    def __init__(self, path):
        self.db = sqlite3.connect(path, timeout=30)
        self.db.executescript(
            """
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY, ticket TEXT, status TEXT, note TEXT);
            CREATE TABLE IF NOT EXISTS steps (
                run_id TEXT, step TEXT, output TEXT, PRIMARY KEY (run_id, step));
            CREATE TABLE IF NOT EXISTS tool_calls (
                op_id TEXT PRIMARY KEY, run_id TEXT, step TEXT,
                status TEXT, request TEXT, result TEXT);
            """
        )
        self.db.commit()

    # runs
    def create_run(self, run_id, ticket):
        self.db.execute("INSERT OR REPLACE INTO runs VALUES (?,?,?,?)", (run_id, json.dumps(ticket), "running", None))
        self.db.commit()

    def run(self, run_id):
        row = self.db.execute("SELECT ticket, status, note FROM runs WHERE run_id=?", (run_id,)).fetchone()
        return {"ticket": json.loads(row[0]), "status": row[1], "note": row[2]}

    def set_run_status(self, run_id, status, note=None):
        self.db.execute("UPDATE runs SET status=?, note=? WHERE run_id=?", (status, note, run_id))
        self.db.commit()

    # steps (the checkpoint proper)
    def completed_steps(self, run_id):
        rows = self.db.execute("SELECT step, output FROM steps WHERE run_id=?", (run_id,)).fetchall()
        return {s: json.loads(o) for s, o in rows}

    def complete_step(self, run_id, step, output):
        self.db.execute("INSERT OR REPLACE INTO steps VALUES (?,?,?)", (run_id, step, json.dumps(output)))
        self.db.commit()

    # intent records
    def tool_call(self, op_id):
        row = self.db.execute("SELECT status, request, result FROM tool_calls WHERE op_id=?", (op_id,)).fetchone()
        if not row:
            return None
        return {"status": row[0], "request": json.loads(row[1]), "result": json.loads(row[2]) if row[2] else None}

    def record_started(self, op_id, run_id, step, request):
        self.db.execute("INSERT OR REPLACE INTO tool_calls VALUES (?,?,?,?,?,?)",
                        (op_id, run_id, step, "started", json.dumps(request), None))
        self.db.commit()

    def record_done(self, op_id, result):
        self.db.execute("UPDATE tool_calls SET status='done', result=? WHERE op_id=?", (json.dumps(result), op_id))
        self.db.commit()

    def runs_by_status(self):
        return dict(self.db.execute("SELECT status, COUNT(*) FROM runs GROUP BY status").fetchall())
