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


LOCK_KEY = 815_204_311  # arbitrary, stable advisory-lock key for this app


@contextmanager
def advisory_lock(con):
    """Postgres session-level advisory lock: works across serverless invocations."""
    got = con.execute("SELECT pg_try_advisory_lock(?) AS ok", (LOCK_KEY,)).fetchone()["ok"]
    if not got:
        raise AlreadyRunning("another lead-engine run holds the database lock")
    try:
        yield
    finally:
        con.execute("SELECT pg_advisory_unlock(?)", (LOCK_KEY,))
        con.commit()


def lock_for(ctx):
    from .storage import PgConnection

    if isinstance(ctx.con, PgConnection):
        return advisory_lock(ctx.con)
    return run_lock(ctx.settings.lock_path)


class Run:
    """Counts ok/failed/skipped items and cost for one command; written to `runs` on exit."""

    def __init__(self, con, command: str):
        self.con = con
        self.command = command
        self.run_id = db.new_id("run")
        self.ok = self.failed = self.skipped = 0
        self.cost_usd = 0.0
        self.notes: list[str] = []
        self.exit_code = 0  # non-zero marks the run 'failed' so it never counts as a success

    def __enter__(self):
        self.con.execute("INSERT INTO runs(run_id,command,started_at,status) VALUES(?,?,?, 'running')",
                         (self.run_id, self.command, db.now()))
        self.con.commit()
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc:
            self.con.rollback()  # never keep half a run (e.g. suppressions not yet pushed to the blocklist)
        status = "error" if exc else ("failed" if self.exit_code else "ok")
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
