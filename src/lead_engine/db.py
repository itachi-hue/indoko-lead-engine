"""SQLite store. The system of record for people, evidence, drafts, sends and events.

Ported from the o1-leadgen prototype (same table and key names: persons.person_id,
signals.signal_id, singular segments), with the additions in docs/DESIGN.md section 4.
All writes are idempotent. Types stay Postgres-compatible (TEXT/INTEGER/REAL, JSON as TEXT).
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import sqlite3
import uuid
from pathlib import Path

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS source_records (
    record_id        TEXT PRIMARY KEY,
    source           TEXT NOT NULL,
    source_native_id TEXT NOT NULL,
    kind             TEXT NOT NULL,
    payload          TEXT NOT NULL,
    fetched_at       TEXT NOT NULL,
    UNIQUE(source, source_native_id)
);
CREATE TABLE IF NOT EXISTS persons (
    person_id        TEXT PRIMARY KEY,
    display_name     TEXT,
    primary_employer TEXT,
    employer_conf    TEXT,
    employer_domain  TEXT,
    country          TEXT,
    us_located       INTEGER,              -- 1 yes, 0 no/unknown
    segment          TEXT,                 -- engineer|founder|researcher|creator|warm
    group_source     TEXT,                 -- warm|cold
    title            TEXT,
    email            TEXT,                 -- chosen, verified address
    email_source     TEXT,
    email_status     TEXT,                 -- verified|catch_all_published|none
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS person_identifiers (
    person_id  TEXT NOT NULL REFERENCES persons(person_id) ON DELETE CASCADE,
    id_type    TEXT NOT NULL,
    id_value   TEXT NOT NULL,
    confidence TEXT DEFAULT 'high',
    created_at TEXT NOT NULL,
    PRIMARY KEY (id_type, id_value)
);
CREATE TABLE IF NOT EXISTS identity_edges (
    edge_id    TEXT PRIMARY KEY,
    a_type TEXT, a_val TEXT, b_type TEXT, b_val TEXT,
    reason TEXT, confidence REAL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS merge_queue (
    queue_id   TEXT PRIMARY KEY,
    new_identifier TEXT,
    candidate_person_ids TEXT,
    reason     TEXT,
    status     TEXT DEFAULT 'open',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS signals (
    signal_id     TEXT PRIMARY KEY,
    person_id     TEXT NOT NULL REFERENCES persons(person_id) ON DELETE CASCADE,
    signal_type   TEXT NOT NULL,
    value         TEXT,
    provenance    TEXT,
    evidence_text TEXT,                    -- human-readable fact a draft may cite
    evidence_date TEXT,                    -- when the fact happened (not fetch time)
    url           TEXT,
    observed_at   TEXT NOT NULL,
    UNIQUE(person_id, signal_type, provenance)
);
CREATE TABLE IF NOT EXISTS scores (
    score_id      TEXT PRIMARY KEY,
    person_id     TEXT NOT NULL REFERENCES persons(person_id) ON DELETE CASCADE,
    score_version TEXT NOT NULL,
    segment       TEXT,
    raw           REAL,
    percentile    REAL,
    subscores     TEXT,
    snapshot_hash TEXT,
    computed_at   TEXT NOT NULL,
    UNIQUE(person_id, score_version)
);
CREATE TABLE IF NOT EXISTS enrichment_ledger (
    ledger_id    TEXT PRIMARY KEY,
    person_id    TEXT,
    provider     TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    cost_usd     REAL DEFAULT 0,
    status       TEXT,                     -- hit|miss|skipped|error
    result       TEXT,
    created_at   TEXT NOT NULL,
    UNIQUE(provider, request_hash)
);
CREATE TABLE IF NOT EXISTS suppression (
    supp_id    TEXT PRIMARY KEY,
    match_type TEXT NOT NULL,              -- email|domain|email_hash
    value      TEXT NOT NULL,
    reason     TEXT,
    source     TEXT,                       -- instantly|crm|manual|forget|bounce
    added_at   TEXT NOT NULL,
    UNIQUE(match_type, value)
);
CREATE TABLE IF NOT EXISTS crawl_cursors (
    source TEXT NOT NULL, ckey TEXT NOT NULL,
    cursor TEXT, updated_at TEXT,
    PRIMARY KEY (source, ckey)
);
CREATE TABLE IF NOT EXISTS drafts (
    draft_id        TEXT PRIMARY KEY,
    person_id       TEXT NOT NULL REFERENCES persons(person_id) ON DELETE CASCADE,
    batch_id        TEXT NOT NULL,
    segment         TEXT NOT NULL,
    channel         TEXT NOT NULL DEFAULT 'email',   -- email|linkedin
    subject TEXT, body TEXT, fu1 TEXT, fu2 TEXT,
    evidence_ids    TEXT,
    model TEXT, prompt_version TEXT,
    checks          TEXT,
    state           TEXT NOT NULL,
    edited_subject TEXT, edited_body TEXT, edited_fu1 TEXT, edited_fu2 TEXT,
    reviewer TEXT, decided_at TEXT, notes TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS drafts_person ON drafts(person_id);
CREATE INDEX IF NOT EXISTS drafts_state ON drafts(state);
CREATE TABLE IF NOT EXISTS sends (
    send_id           TEXT PRIMARY KEY,
    draft_id          TEXT NOT NULL UNIQUE REFERENCES drafts(draft_id) ON DELETE CASCADE,
    campaign_id       TEXT NOT NULL,
    instantly_lead_id TEXT,
    email             TEXT,
    method            TEXT NOT NULL,       -- api|csv
    status            TEXT NOT NULL,       -- pending|sent|exported|failed
    attempted_at TEXT, sent_at TEXT, removed_at TEXT, error TEXT
);
CREATE TABLE IF NOT EXISTS events (
    event_id    TEXT PRIMARY KEY,
    person_id   TEXT NOT NULL REFERENCES persons(person_id) ON DELETE CASCADE,
    draft_id    TEXT REFERENCES drafts(draft_id) ON DELETE CASCADE,
    type        TEXT NOT NULL,
    source      TEXT NOT NULL,             -- instantly|crm|manual|csv
    external_id TEXT UNIQUE,
    occurred_at TEXT NOT NULL,
    payload     TEXT
);
CREATE INDEX IF NOT EXISTS events_person ON events(person_id);
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY, command TEXT NOT NULL,
    started_at TEXT NOT NULL, finished_at TEXT,
    ok INTEGER DEFAULT 0, failed INTEGER DEFAULT 0, skipped INTEGER DEFAULT 0,
    cost_usd REAL DEFAULT 0, status TEXT, error TEXT
);
"""

EVENT_TYPES = (
    "sent", "reply", "positive_reply", "bounce", "unsubscribe", "booked", "attended",
    "onepager_sent", "client", "not_interested",
)


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def stable_id(*parts) -> str:
    return hashlib.sha1("||".join(str(p) for p in parts).encode()).hexdigest()[:20]


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


def connect(path: Path | str) -> sqlite3.Connection:
    path = Path(path)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path), timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    if str(path) != ":memory:":
        con.execute("PRAGMA journal_mode=WAL")
    return con


def init_db(con: sqlite3.Connection) -> None:
    con.executescript(SCHEMA)
    con.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    con.commit()


# ---------- identifiers ----------
_PATHY = {"openalex", "orcid", "semanticscholar", "github", "exa"}
_LOWER = {"openalex", "orcid", "github", "email", "semanticscholar", "linkedin"}


def norm_identifier(id_type: str, value) -> str:
    v = str(value).strip()
    if id_type in _PATHY and "/" in v and id_type != "exa":
        v = v.rstrip("/").rsplit("/", 1)[-1].strip()
    if id_type == "linkedin":
        v = v.lower().rstrip("/").split("?")[0]
        v = v.replace("https://", "").replace("http://", "").replace("www.", "")
    if id_type in _LOWER:
        v = v.lower()
    return v


def upsert_source_record(con, source, native_id, kind, payload) -> str:
    native_id = str(native_id).strip()
    rid = stable_id(source, native_id)
    con.execute(
        "INSERT INTO source_records(record_id,source,source_native_id,kind,payload,fetched_at) "
        "VALUES(?,?,?,?,?,?) ON CONFLICT(source,source_native_id) DO UPDATE SET "
        "payload=excluded.payload, fetched_at=excluded.fetched_at",
        (rid, source, native_id, kind, json.dumps(payload, default=str), now()))
    return rid


def find_person_by_identifier(con, id_type, id_value):
    r = con.execute("SELECT person_id FROM person_identifiers WHERE id_type=? AND id_value=?",
                    (id_type, norm_identifier(id_type, id_value))).fetchone()
    return r["person_id"] if r else None


def add_identifier(con, person_id, id_type, id_value, confidence="high") -> str:
    """Returns 'ok' | 'exists' | 'conflict' (identifier already bound to another person)."""
    id_value = norm_identifier(id_type, id_value)
    owner = con.execute("SELECT person_id FROM person_identifiers WHERE id_type=? AND id_value=?",
                        (id_type, id_value)).fetchone()
    if owner:
        return "exists" if owner["person_id"] == person_id else "conflict"
    con.execute("INSERT INTO person_identifiers(person_id,id_type,id_value,confidence,created_at) "
                "VALUES(?,?,?,?,?)", (person_id, id_type, id_value, confidence, now()))
    return "ok"


PERSON_COLS = {"display_name", "primary_employer", "employer_conf", "employer_domain", "country",
               "us_located", "segment", "group_source", "title", "email", "email_source",
               "email_status"}


def create_person(con, display_name, **fields) -> str:
    pid = new_id("p")
    fields = {k: v for k, v in fields.items() if k in PERSON_COLS}
    cols = ["person_id", "display_name", *fields.keys(), "created_at", "updated_at"]
    vals = [pid, display_name, *fields.values(), now(), now()]
    con.execute(f"INSERT INTO persons({','.join(cols)}) VALUES({','.join('?' * len(cols))})", vals)
    return pid


def touch_person(con, person_id, **fields) -> None:
    fields = {k: v for k, v in fields.items() if k in PERSON_COLS}
    if not fields:
        return
    sets = ", ".join(f"{k}=?" for k in fields) + ", updated_at=?"
    con.execute(f"UPDATE persons SET {sets} WHERE person_id=?", (*fields.values(), now(), person_id))


def get_person(con, person_id):
    return con.execute("SELECT * FROM persons WHERE person_id=?", (person_id,)).fetchone()


def enqueue_merge(con, new_identifier, candidate_person_ids, reason) -> str:
    qid = stable_id("merge", json.dumps(new_identifier, sort_keys=True),
                    json.dumps(sorted(candidate_person_ids)))
    con.execute("INSERT INTO merge_queue(queue_id,new_identifier,candidate_person_ids,reason,status,"
                "created_at) VALUES(?,?,?,?, 'open', ?) ON CONFLICT(queue_id) DO UPDATE SET "
                "reason=excluded.reason",
                (qid, json.dumps(new_identifier), json.dumps(candidate_person_ids), reason, now()))
    return qid


def add_edge(con, a_type, a_val, b_type, b_val, reason, confidence=1.0) -> None:
    a, b = norm_identifier(a_type, a_val), norm_identifier(b_type, b_val)
    eid = stable_id("edge", a_type, a, b_type, b, reason)
    con.execute("INSERT INTO identity_edges(edge_id,a_type,a_val,b_type,b_val,reason,confidence,"
                "created_at) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(edge_id) DO NOTHING",
                (eid, a_type, a, b_type, b, reason, confidence, now()))


# ---------- evidence ----------
def add_signal(con, person_id, signal_type, value, provenance, evidence_text=None,
               evidence_date=None, url=None) -> str:
    sid = stable_id(person_id, signal_type, provenance)
    con.execute(
        "INSERT INTO signals(signal_id,person_id,signal_type,value,provenance,evidence_text,"
        "evidence_date,url,observed_at) VALUES(?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(person_id,signal_type,provenance) DO UPDATE SET value=excluded.value, "
        "evidence_text=COALESCE(excluded.evidence_text, signals.evidence_text), "
        "evidence_date=COALESCE(excluded.evidence_date, signals.evidence_date), "
        "url=COALESCE(excluded.url, signals.url), observed_at=excluded.observed_at",
        (sid, person_id, signal_type, None if value is None else str(value), provenance,
         evidence_text, evidence_date, url, now()))
    return sid


def evidence_for(con, person_id) -> list[sqlite3.Row]:
    """Citable evidence (has text and a source URL), newest first."""
    return list(con.execute(
        "SELECT * FROM signals WHERE person_id=? AND evidence_text IS NOT NULL AND url IS NOT NULL "
        "ORDER BY COALESCE(evidence_date,'') DESC, observed_at DESC", (person_id,)))


def record_score(con, person_id, version, segment, raw, percentile, subscores, snapshot_hash):
    con.execute(
        "INSERT INTO scores(score_id,person_id,score_version,segment,raw,percentile,subscores,"
        "snapshot_hash,computed_at) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(person_id,score_version) "
        "DO UPDATE SET raw=excluded.raw, percentile=excluded.percentile, "
        "subscores=excluded.subscores, snapshot_hash=excluded.snapshot_hash, "
        "computed_at=excluded.computed_at",
        (stable_id(person_id, version), person_id, version, segment, raw, percentile,
         json.dumps(subscores), snapshot_hash, now()))


# ---------- ledger ----------
def ledger_spent(con, since: str | None = None) -> float:
    q, args = "SELECT COALESCE(SUM(cost_usd),0) s FROM enrichment_ledger", ()
    if since:
        q += " WHERE created_at >= ?"
        args = (since,)
    return float(con.execute(q, args).fetchone()["s"])


def ledger_seen(con, provider, request_hash):
    r = con.execute("SELECT status, result FROM enrichment_ledger WHERE provider=? AND request_hash=?",
                    (provider, request_hash)).fetchone()
    if not r:
        return None
    return {"status": r["status"], "result": json.loads(r["result"]) if r["result"] else None}


def ledger_add(con, person_id, provider, request_hash, cost, status, result) -> None:
    con.execute("INSERT INTO enrichment_ledger(ledger_id,person_id,provider,request_hash,cost_usd,"
                "status,result,created_at) VALUES(?,?,?,?,?,?,?,?) "
                "ON CONFLICT(provider,request_hash) DO NOTHING",
                (stable_id(provider, request_hash), person_id, provider, request_hash, cost, status,
                 json.dumps(result, default=str), now()))


# ---------- events ----------
def add_event(con, person_id, type_, source, occurred_at=None, draft_id=None, external_id=None,
              payload=None) -> bool:
    """Insert an event. Returns False if external_id was already recorded (repeated poll)."""
    if type_ not in EVENT_TYPES:
        raise ValueError(f"unknown event type: {type_}")
    if external_id:
        exists = con.execute("SELECT 1 FROM events WHERE external_id=?", (external_id,)).fetchone()
        if exists:
            return False
    con.execute("INSERT INTO events(event_id,person_id,draft_id,type,source,external_id,occurred_at,"
                "payload) VALUES(?,?,?,?,?,?,?,?)",
                (new_id("ev"), person_id, draft_id, type_, source, external_id,
                 occurred_at or now(), json.dumps(payload) if payload is not None else None))
    return True


# ---------- cursors ----------
def get_cursor(con, source, ckey):
    r = con.execute("SELECT cursor FROM crawl_cursors WHERE source=? AND ckey=?", (source, ckey)).fetchone()
    return r["cursor"] if r else None


def set_cursor(con, source, ckey, cursor) -> None:
    con.execute("INSERT INTO crawl_cursors(source,ckey,cursor,updated_at) VALUES(?,?,?,?) "
                "ON CONFLICT(source,ckey) DO UPDATE SET cursor=excluded.cursor, "
                "updated_at=excluded.updated_at", (source, ckey, cursor, now()))
