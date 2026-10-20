import httpx
import pytest

from lead_engine import db, http
from lead_engine.cli import main
from lead_engine.config import load_settings, parse_env_file
from lead_engine.runtime import AlreadyRunning, Run, last_success, run_lock
from lead_engine.sheets.layout import col_letter


def test_env_file_parsing(tmp_path):
    p = tmp_path / ".env"
    p.write_text('# comment\nA=1\nB="two words"\nC=\'x\'\nbad line\n')
    assert parse_env_file(p) == {"A": "1", "B": "two words", "C": "x"}


def test_settings_merge_and_secret_precedence(root):
    s = load_settings(root, env={"EXA_API_KEY": "from-env"})
    assert s.secret("EXA_API_KEY") == "from-env"          # environment beats .env
    assert s.secret("GEMINI_API_KEY") == "gem-k"           # falls back to .env
    assert s.get("sender.name") == "Karishma"              # from config.toml
    assert s.get("sending.bounce_pause_pct") == 2.0        # default kept
    assert s.get("instantly.campaigns.engineer") == "camp-eng"


def test_init_db_is_idempotent(tmp_path):
    con = db.connect(tmp_path / "x.sqlite")
    db.init_db(con)
    db.init_db(con)
    tables = {r["name"] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"persons", "signals", "drafts", "sends", "events", "runs", "suppression"} <= tables


def test_identifiers_normalize_and_detect_conflicts(con):
    a = db.create_person(con, "A")
    b = db.create_person(con, "B")
    assert db.add_identifier(con, a, "github", "https://github.com/AdaL") == "ok"
    assert db.find_person_by_identifier(con, "github", "adal") == a
    assert db.add_identifier(con, a, "github", "adal") == "exists"
    assert db.add_identifier(con, b, "github", "ADAL") == "conflict"


def test_signal_upsert_keeps_evidence(con):
    pid = db.create_person(con, "A")
    db.add_signal(con, pid, "patent", "1", "exa:u1", evidence_text="Patent US123", url="https://x/1")
    db.add_signal(con, pid, "patent", "2", "exa:u1")  # refresh without text keeps the text
    rows = db.evidence_for(con, pid)
    assert len(rows) == 1 and rows[0]["evidence_text"] == "Patent US123" and rows[0]["value"] == "2"


def test_event_external_id_dedupes(con):
    pid = db.create_person(con, "A")
    assert db.add_event(con, pid, "reply", "instantly", external_id="e1") is True
    assert db.add_event(con, pid, "reply", "instantly", external_id="e1") is False
    with pytest.raises(ValueError):
        db.add_event(con, pid, "opened", "instantly")


def test_http_retries_then_succeeds():
    attempts = {"n": 0}

    def handler(req):
        attempts["n"] += 1
        if attempts["n"] < 3:
            return httpx.Response(429, headers={"Retry-After": "1"})
        return httpx.Response(200, json={"ok": True})

    client = http.make_client(transport=httpx.MockTransport(handler))
    slept = []
    assert http.get_json(client, "https://x/y", sleep=slept.append) == {"ok": True}
    assert attempts["n"] == 3 and slept == [1.0, 1.0]


def test_http_fails_fast_on_4xx():
    client = http.make_client(transport=httpx.MockTransport(lambda r: httpx.Response(403, text="nope")))
    with pytest.raises(http.HTTPError) as e:
        http.get_json(client, "https://x/y", sleep=lambda s: None)
    assert e.value.status == 403


def test_http_gives_up_after_max_retries():
    client = http.make_client(transport=httpx.MockTransport(lambda r: httpx.Response(503)))
    slept = []
    with pytest.raises(http.HTTPError):
        http.get_json(client, "https://x/y", sleep=slept.append)
    assert len(slept) == http.MAX_RETRIES


def test_lock_blocks_second_run(tmp_path):
    lock = tmp_path / "l.lock"
    with run_lock(lock):
        with pytest.raises(AlreadyRunning):
            with run_lock(lock):
                pass
    with run_lock(lock):  # released after exit
        pass


def test_run_records_counts_and_errors(con):
    with Run(con, "demo") as r:
        r.ok += 2
        r.skipped += 1
        r.cost_usd += 0.5
    row = con.execute("SELECT * FROM runs WHERE command='demo'").fetchone()
    assert (row["ok"], row["skipped"], row["cost_usd"], row["status"]) == (2, 1, 0.5, "ok")
    assert last_success(con, "demo")
    with pytest.raises(RuntimeError):
        with Run(con, "boom"):
            raise RuntimeError("bad")
    assert con.execute("SELECT status FROM runs WHERE command='boom'").fetchone()["status"] == "error"
    assert last_success(con, "boom") is None


def test_col_letter():
    assert [col_letter(i) for i in (0, 25, 26, 27, 51, 52)] == ["A", "Z", "AA", "AB", "AZ", "BA"]


def test_check_reports_missing_secret(ctx, capsys):
    ctx.settings.secrets["GOOGLE_SERVICE_ACCOUNT_FILE"] = ""
    assert main(["check"], ctx=ctx) == 1
    out = capsys.readouterr().out
    assert '"GOOGLE_SERVICE_ACCOUNT_FILE": false' in out


def test_check_live_uses_each_api(ctx, router, capsys):
    ctx.settings.secrets["GOOGLE_SERVICE_ACCOUNT_FILE"] = "/tmp/key.json"
    router.add("GET", r"generativelanguage.*/models", {"models": [{"name": "models/gemini-test"}]})
    router.add("GET", r"api.instantly.ai/api/v2/campaigns", {"items": [{"id": "c1"}]})
    router.add("GET", r"sheets.googleapis.com/v4/spreadsheets/sheet123", {"properties": {"title": "Indoko"}})
    router.add("POST", r"api.exa.ai/search", {"results": []})
    assert main(["check", "--live"], ctx=ctx) == 0
    out = capsys.readouterr().out
    assert '"model_available": true' in out and '"title": "Indoko"' in out


def test_check_live_reports_instantly_without_api(ctx, router, capsys):
    ctx.settings.secrets["GOOGLE_SERVICE_ACCOUNT_FILE"] = "/tmp/key.json"
    router.add("GET", r"generativelanguage.*/models", {"models": [{"name": "models/gemini-test"}]})
    router.add("GET", r"api.instantly.ai", {"error": "plan"}, status=403)
    router.add("GET", r"sheets.googleapis.com", {"properties": {"title": "Indoko"}})
    router.add("POST", r"api.exa.ai/search", {"results": []})
    assert main(["check", "--live"], ctx=ctx) == 1
    assert '"status": 403' in capsys.readouterr().out
