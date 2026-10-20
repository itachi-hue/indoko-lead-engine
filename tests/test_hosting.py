import importlib.util
import json
import sys
from pathlib import Path

import pytest

from lead_engine import storage
from lead_engine.config import load_settings
from lead_engine.runtime import AlreadyRunning, advisory_lock, lock_for


def test_translate_placeholders_and_percent():
    assert storage.translate("SELECT * FROM t WHERE a=? AND b LIKE ?") == "SELECT * FROM t WHERE a=%s AND b LIKE %s"
    assert storage.translate("SELECT '?' , ? , 'it''s %'") == "SELECT '?' , %s , 'it''s %%'"
    assert storage.translate("SELECT 5 % 2") == "SELECT 5 %% 2"


def test_row_supports_name_and_index():
    r = storage.Row(["a", "b"], [1, 2])
    assert r["a"] == 1 and r[1] == 2 and list(r.keys()) == ["a", "b"]


class FakePgRaw:
    """Stands in for a psycopg connection to test the adapter's behaviour."""

    def __init__(self):
        self.executed, self.commits, self.rollbacks, self.closed = [], 0, 0, False

    def cursor(self):
        raw = self

        class C:
            description = [type("D", (), {"name": "ok"})()]
            rowcount = 1

            def execute(self, sql, params):
                raw.executed.append((sql, params))

            def fetchone(self):
                return (True,)

            def fetchall(self):
                return [(True,)]
        return C()

    def execute(self, sql):
        self.executed.append((sql, None))

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


def test_pg_adapter_transactions_do_not_close():
    raw = FakePgRaw()
    con = storage.PgConnection(raw)
    with con:
        con.execute("UPDATE t SET a=? WHERE b=?", (1, 2))
    assert raw.commits == 1 and not raw.closed
    assert raw.executed[0] == ("UPDATE t SET a=%s WHERE b=%s", (1, 2))
    with pytest.raises(ValueError):
        with con:
            raise ValueError("boom")
    assert raw.rollbacks == 1 and not raw.closed


def test_pg_schema_skips_pragma():
    raw = FakePgRaw()
    storage.PgConnection(raw).executescript("CREATE TABLE a(x TEXT); PRAGMA foo=1; CREATE TABLE b(y TEXT);")
    sqls = [s.strip() for s, _ in raw.executed]
    assert sqls == ["CREATE TABLE a(x TEXT)", "CREATE TABLE b(y TEXT)"]


def test_advisory_lock_and_selection(ctx):
    raw = FakePgRaw()
    con = storage.PgConnection(raw)
    with advisory_lock(con):
        pass
    assert any("pg_try_advisory_lock" in s for s, _ in raw.executed)
    assert any("pg_advisory_unlock" in s for s, _ in raw.executed)
    ctx.con = con
    assert lock_for(ctx).__class__.__name__ == "_GeneratorContextManager"


def test_advisory_lock_busy():
    con = storage.PgConnection(FakePgRaw())
    con.execute = lambda sql, params=(): type("C", (), {"fetchone": lambda self: {"ok": False}})()
    with pytest.raises(AlreadyRunning):
        with advisory_lock(con):
            pass


def test_config_from_env_var(tmp_path):
    toml = '[sender]\nname = "Karishma"\n[storage]\nbackend = "postgres"\n'
    s = load_settings(tmp_path, env={"LEAD_ENGINE_CONFIG_TOML": toml, "DATABASE_URL": "postgres://x",
                                     "GOOGLE_SERVICE_ACCOUNT_JSON": '{"client_email": "a"}'})
    assert s.get("sender.name") == "Karishma" and s.get("storage.backend") == "postgres"
    assert s.secret("DATABASE_URL") == "postgres://x" and json.loads(s.secret("GOOGLE_SERVICE_ACCOUNT_JSON"))


def test_postgres_without_url_errors(tmp_path):
    s = load_settings(tmp_path, env={"LEAD_ENGINE_CONFIG_TOML": '[storage]\nbackend = "postgres"\n'})
    with pytest.raises(RuntimeError):
        storage.connect(s)


def _cron_module():
    path = Path(__file__).resolve().parents[1] / "api" / "cron.py"
    spec = importlib.util.spec_from_file_location("cron", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_cron_auth():
    cron = _cron_module()
    assert cron.authorized("Bearer s3cret", "s3cret")
    assert not cron.authorized("Bearer wrong", "s3cret")
    assert not cron.authorized(None, "s3cret")
    assert not cron.authorized("Bearer ", "")            # no secret configured -> always refuse
    assert "send" not in cron.ALLOWED_JOBS and "run-daily" in cron.ALLOWED_JOBS
