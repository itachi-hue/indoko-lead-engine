"""Storage backends behind one connection interface (docs/DESIGN.md section 12).

The pipeline talks to a sqlite3-style connection: execute() with `?` placeholders, rows readable
by column name or index, commit(), and `with con:` as a transaction. SQLite provides that
natively. For Postgres (hosting, M6) PgConnection adapts psycopg 3 to the same interface.

Untested against a live Postgres server at the time of writing: verify on first deploy with
`lead-engine init` and the end-to-end test in docs/DESIGN.md section 15.
"""
from __future__ import annotations

import re

from . import db

_DDL_SKIP = re.compile(r"^\s*PRAGMA\b", re.I)


def translate(sql: str) -> str:
    """`?` placeholders -> `%s`, and literal `%` -> `%%`, ignoring anything inside quotes."""
    out, quote = [], None
    for ch in sql:
        if quote:
            out.append("%%" if ch == "%" else ch)
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
            out.append(ch)
        elif ch == "?":
            out.append("%s")
        elif ch == "%":
            out.append("%%")
        else:
            out.append(ch)
    return "".join(out)


class Row(dict):
    """A dict that also supports positional access, like sqlite3.Row."""

    def __init__(self, cols, values):
        super().__init__(zip(cols, values))
        self._values = list(values)

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._values[key]
        return super().__getitem__(key)


class _Cursor:
    def __init__(self, cur):
        self._cur = cur

    @property
    def rowcount(self):
        return self._cur.rowcount

    def _cols(self):
        return [d.name for d in (self._cur.description or [])]

    def fetchone(self):
        r = self._cur.fetchone()
        return Row(self._cols(), r) if r is not None else None

    def fetchall(self):
        cols = self._cols()
        return [Row(cols, r) for r in self._cur.fetchall()]

    def __iter__(self):
        return iter(self.fetchall())


class PgConnection:
    def __init__(self, raw):
        self.raw = raw  # psycopg.Connection, autocommit off

    def execute(self, sql: str, params=()):
        cur = self.raw.cursor()
        cur.execute(translate(sql), tuple(params) if params else None)
        return _Cursor(cur)

    def executescript(self, script: str) -> None:
        for stmt in script.split(";"):
            if stmt.strip() and not _DDL_SKIP.match(stmt):
                self.raw.execute(stmt)
        self.raw.commit()

    def commit(self):
        self.raw.commit()

    def rollback(self):
        self.raw.rollback()

    def close(self):
        self.raw.close()

    # sqlite3 semantics: `with con:` commits or rolls back, and does NOT close
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type:
            self.raw.rollback()
        else:
            self.raw.commit()
        return False


def connect(settings):
    backend = settings.get("storage.backend", "sqlite")
    if backend == "sqlite":
        return db.connect(settings.db_path)
    if backend == "postgres":
        try:
            import psycopg
        except ModuleNotFoundError as exc:
            raise RuntimeError("install the postgres extra: uv sync --extra postgres") from exc
        url = settings.secret("DATABASE_URL") or settings.get("storage.database_url", "")
        if not url:
            raise RuntimeError("DATABASE_URL is not set")
        return PgConnection(psycopg.connect(url))
    raise RuntimeError(f"unknown storage backend: {backend}")


def init(con) -> None:
    if isinstance(con, PgConnection):
        con.executescript(db.SCHEMA)
    else:
        db.init_db(con)
