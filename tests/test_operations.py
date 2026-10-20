import datetime as dt
from pathlib import Path

import pytest

from lead_engine import db, maintenance, report
from lead_engine.runtime import Run

from conftest import make_person
from fakes import FakeSheets


def test_wilson_interval_matches_reference():
    lo, hi = report.wilson(3, 100)
    assert round(lo, 3) == 0.010 and round(hi, 3) == 0.085   # the "about 1% to 8.5%" in the design
    assert report.wilson(0, 0) == (0.0, 0.0)
    lo, hi = report.wilson(0, 50)
    assert lo == 0.0 and 0.06 < hi < 0.08


def _sent(con, pid, when, seg="founder"):
    did = db.new_id("d")
    con.execute("INSERT INTO drafts(draft_id,person_id,batch_id,segment,state,created_at,updated_at) "
                "VALUES(?,?,?,?,'sent',?,?)", (did, pid, "b", seg, when, when))
    con.execute("INSERT INTO sends(send_id,draft_id,campaign_id,method,status,sent_at,attempted_at) "
                "VALUES(?,?,?,'api','sent',?,?)", (db.new_id("s"), did, "c", when, when))
    db.add_event(con, pid, "sent", "instantly", occurred_at=when)
    return did


def test_metrics_by_week_group_and_source(con):
    when = "2026-11-24T10:00:00Z"
    people = [make_person(con, name=f"P {i}", segment="founder", email=f"p{i}@x.ai") for i in range(4)]
    for p in people:
        _sent(con, p, when)
    db.add_event(con, people[0], "bounce", "instantly")
    db.add_event(con, people[1], "reply", "instantly")
    db.add_event(con, people[1], "positive_reply", "instantly")
    db.add_event(con, people[1], "positive_reply", "crm", external_id="dup")   # counted once per person
    db.add_event(con, people[1], "client", "crm")
    con.execute("INSERT INTO runs(run_id,command,started_at,cost_usd,status) VALUES('r','x',?,12.0,'ok')", (when,))
    con.commit()
    (row,) = report.compute(con)
    assert (row["week"], row["group"], row["source"]) == ("2026-W48", "founder", "cold")
    assert (row["sent"], row["bounce"], row["delivered"], row["positive_reply"], row["client"]) == (4, 1, 3, 1, 1)
    assert row["spend"] == 12.0 and row["cost_per_client"] == 12.0


def test_metrics_write_preserves_manual_complaint_column(ctx):
    con = ctx.con
    p = make_person(con, segment="founder")
    _sent(con, p, "2026-11-24T10:00:00Z")
    con.commit()
    ctx.sheets = FakeSheets()
    from lead_engine.sheets.layout import METRICS_COLUMNS
    ctx.sheets.tabs["Metrics"] = [list(METRICS_COLUMNS)]
    with Run(con, "report") as r:
        report.write(ctx, r)
    ctx.sheets.tabs["Metrics"][1][-1] = "0.05%"                       # entered by hand
    db.add_event(con, p, "reply", "instantly")
    con.commit()
    with Run(con, "report") as r:
        report.write(ctx, r)
    rows = ctx.sheets.tabs["Metrics"]
    assert len(rows) == 2 and rows[1][-1] == "0.05%" and rows[1][METRICS_COLUMNS.index("Replies")] == "1"


def test_retention_deletes_only_old_uncontacted(ctx):
    con = ctx.con
    old_untouched = make_person(con, name="Old A", email="a@x.ai")
    old_contacted = make_person(con, name="Old B", email="b@x.ai")
    old_in_review = make_person(con, name="Old C", email="c@x.ai")
    new_untouched = make_person(con, name="New D", email="d@x.ai")
    con.execute("UPDATE persons SET created_at='2026-01-01T00:00:00Z' WHERE person_id IN (?,?,?)",
                (old_untouched, old_contacted, old_in_review))
    _sent(con, old_contacted, "2026-02-01T00:00:00Z")
    con.execute("INSERT INTO drafts(draft_id,person_id,batch_id,segment,state,created_at,updated_at) "
                "VALUES('dx',?,'b','founder','pushed','x','x')", (old_in_review,))
    con.commit()
    with Run(con, "retention") as r:
        n = maintenance.retention(ctx, r, today=dt.date(2026, 11, 1))
    assert n == 1
    remaining = {r["person_id"] for r in con.execute("SELECT person_id FROM persons")}
    assert remaining == {old_contacted, old_in_review, new_untouched}


def test_backup_refuses_without_age(ctx, monkeypatch):
    ctx.settings.data["drive"]["backup_folder_id"] = "folder"
    ctx.settings.data["backup"] = {"age_recipient": "age1xyz"}
    monkeypatch.setattr(maintenance.shutil, "which", lambda name: None)
    with Run(ctx.con, "backup") as r, pytest.raises(maintenance.BackupError, match="unencrypted"):
        maintenance.backup(ctx, r)


def test_backup_rejects_bad_recipient(ctx, monkeypatch, tmp_path):
    monkeypatch.setattr(maintenance.shutil, "which", lambda name: "/usr/bin/age")
    with pytest.raises(maintenance.BackupError, match="age public key"):
        maintenance.encrypt(tmp_path / "a", tmp_path / "b", "not-a-key")


class FakeDrive:
    def __init__(self, files):
        self.files, self.uploaded, self.deleted = files, [], []

    def upload(self, folder, name, content):
        self.uploaded.append((folder, name, content))
        return "new-id"

    def list_folder(self, folder):
        return self.files

    def delete(self, fid):
        self.deleted.append(fid)


def test_backup_uploads_encrypted_and_prunes(ctx, monkeypatch):
    ctx.settings.data["drive"]["backup_folder_id"] = "folder"
    ctx.settings.data["backup"] = {"age_recipient": "age1xyz"}

    def fake_encrypt(src: Path, dest: Path, recipient):
        assert src.read_bytes()[:15] == b"SQLite format 3"
        dest.write_bytes(b"age-encryption.org/v1 ...")

    monkeypatch.setattr(maintenance, "encrypt", fake_encrypt)
    ctx.drive = FakeDrive([{"id": "old", "name": "lead-engine-20250101.sqlite.age", "createdTime": "2025-01-01T00:00:00"},
                           {"id": "keep", "name": "lead-engine-new.sqlite.age", "createdTime": "2099-01-01T00:00:00"},
                           {"id": "other", "name": "unrelated.pdf", "createdTime": "2020-01-01T00:00:00"}])
    with Run(ctx.con, "backup") as r:
        out = maintenance.backup(ctx, r)
    assert out == {"uploaded": "new-id", "pruned": 1} and ctx.drive.deleted == ["old"]
    assert ctx.drive.uploaded[0][2].startswith(b"age-encryption")
