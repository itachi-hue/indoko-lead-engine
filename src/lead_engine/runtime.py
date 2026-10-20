"""Run bookkeeping: a single-run file lock and a record of every command in the runs table."""
from __future__ import annotations

import fcntl
import logging
import os
from contextlib import contextmanager
from pathlib import Path

from . import db

log = logging.getLogger("lead_engine")


class AlreadyRunning(Exception):
    pass


@contextmanager
def run_lock(path: Path):
    """Exclusive, non-blocking lock. A second run raises AlreadyRunning."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a+")
    try:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise AlreadyRunning(f"another lead-engine command holds {path}") from exc
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()
        yield
    finally:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            fh.close()


class Run:
    """Counts ok/failed/skipped items and cost for one command; written to `runs` on exit."""

    def __init__(self, con, command: str):
        self.con = con
        self.command = command
        self.run_id = db.new_id("run")
        self.ok = self.failed = self.skipped = 0
        self.cost_usd = 0.0
        self.notes: list[str] = []

    def __enter__(self):
        self.con.execute("INSERT INTO runs(run_id,command,started_at,status) VALUES(?,?,?, 'running')",
                         (self.run_id, self.command, db.now()))
        self.con.commit()
        return self

    def __exit__(self, exc_type, exc, tb):
        status = "error" if exc else "ok"
        error = f"{exc_type.__name__}: {exc}" if exc else ("; ".join(self.notes) or None)
        self.con.execute("UPDATE runs SET finished_at=?, ok=?, failed=?, skipped=?, cost_usd=?, "
                         "status=?, error=? WHERE run_id=?",
                         (db.now(), self.ok, self.failed, self.skipped, round(self.cost_usd, 4),
                          status, error, self.run_id))
        self.con.commit()
        log.info("%s: ok=%d failed=%d skipped=%d cost=$%.2f status=%s", self.command, self.ok,
                 self.failed, self.skipped, self.cost_usd, status)
        return False

    def note(self, msg: str) -> None:
        self.notes.append(msg)
        log.warning("%s: %s", self.command, msg)


def last_success(con, command: str) -> str | None:
    r = con.execute("SELECT MAX(finished_at) t FROM runs WHERE command=? AND status='ok'",
                    (command,)).fetchone()
    return r["t"] if r and r["t"] else None
