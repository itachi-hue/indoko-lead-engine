"""Backups and retention (docs/DESIGN.md section 10).

Backups: SQLite online backup -> encrypted with `age` to the configured recipient -> uploaded to a
Shared Drive folder -> copies older than 14 days deleted. Nothing is ever uploaded unencrypted.
Retention: people never contacted are deleted after 90 days.
"""
from __future__ import annotations

import datetime as dt
import shutil
import sqlite3
import subprocess
import tempfile
from pathlib import Path


class BackupError(Exception):
    pass


def snapshot(con, dest: Path) -> None:
    target = sqlite3.connect(str(dest))
    with target:
        con.backup(target)
    target.close()


def encrypt(src: Path, dest: Path, recipient: str) -> None:
    age = shutil.which("age")
    if not age:
        raise BackupError("`age` is not installed (brew install age); refusing to upload an unencrypted backup")
    if not recipient.startswith("age1"):
        raise BackupError("backup.age_recipient must be an age public key (age1...)")
    subprocess.run([age, "-r", recipient, "-o", str(dest), str(src)], check=True, capture_output=True)


def backup(ctx, run) -> dict:
    s = ctx.settings
    folder = s.get("drive.backup_folder_id", "")
    recipient = s.get("backup.age_recipient", "")
    if not folder:
        raise BackupError("drive.backup_folder_id is not set (use a folder in a Shared Drive)")
    with tempfile.TemporaryDirectory() as tmp:
        raw = Path(tmp) / "lead_engine.sqlite"
        enc = Path(tmp) / "lead_engine.sqlite.age"
        snapshot(ctx.con, raw)
        encrypt(raw, enc, recipient)
        raw.unlink()
        name = f"lead-engine-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}.sqlite.age"
        if ctx.dry_run:
            return {"uploaded": None, "pruned": 0}
        file_id = ctx.drive.upload(folder, name, enc.read_bytes())
    keep_days = int(s.get("retention.backup_days", 14))
    cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=keep_days)).strftime("%Y-%m-%dT%H:%M:%S")
    pruned = 0
    for f in ctx.drive.list_folder(folder):
        if f["name"].startswith("lead-engine-") and f.get("createdTime", "") < cutoff:
            ctx.drive.delete(f["id"])
            pruned += 1
    run.ok += 1
    return {"uploaded": file_id, "pruned": pruned}


def retention(ctx, run, today: dt.date | None = None) -> int:
    """Delete people never contacted whose record is older than the retention window."""
    days = int(ctx.settings.get("retention.uncontacted_days", 90))
    cutoff = ((today or dt.date.today()) - dt.timedelta(days=days)).isoformat()
    con = ctx.con
    rows = con.execute(
        "SELECT person_id FROM persons p WHERE substr(created_at,1,10) < ? "
        "AND NOT EXISTS (SELECT 1 FROM drafts d JOIN sends s ON s.draft_id=d.draft_id WHERE d.person_id=p.person_id) "
        "AND NOT EXISTS (SELECT 1 FROM events e WHERE e.person_id=p.person_id) "
        "AND NOT EXISTS (SELECT 1 FROM drafts d WHERE d.person_id=p.person_id AND d.state IN "
        "('pushed','approved','edited','queued'))", (cutoff,)).fetchall()
    if ctx.dry_run:
        print(f"[dry-run] {len(rows)} never-contacted people would be deleted")
        return len(rows)
    with con:
        for r in rows:
            con.execute("DELETE FROM enrichment_ledger WHERE person_id=?", (r["person_id"],))
            con.execute("DELETE FROM persons WHERE person_id=?", (r["person_id"],))
    run.ok += len(rows)
    return len(rows)
