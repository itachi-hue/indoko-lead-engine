"""Identity resolution, ported from the o1-leadgen prototype.

A record whose identifiers match exactly one existing person attaches to that person. A record
whose identifiers point at more than one existing person is never auto-merged: it gets a new
provisional person and the conflict goes to merge_queue for a human to resolve.
"""
from __future__ import annotations

from . import db


def resolve_person(con, identifiers: list[tuple[str, str]], name: str, **fields) -> str:
    identifiers = [(t, v) for t, v in identifiers if v]
    matched: dict[str, list] = {}
    for t, v in identifiers:
        pid = db.find_person_by_identifier(con, t, v)
        if pid:
            matched.setdefault(pid, []).append((t, v))
    distinct = list(matched)
    if not distinct:
        pid = db.create_person(con, name, **fields)
    elif len(distinct) == 1:
        pid = distinct[0]
        row = db.get_person(con, pid)
        fill = {k: v for k, v in fields.items() if v not in (None, "") and row[k] in (None, "")}
        if fill:
            db.touch_person(con, pid, **fill)
    else:
        pid = db.create_person(con, name, **fields)
        db.enqueue_merge(con, [{"id_type": t, "id_value": v} for t, v in identifiers],
                         distinct + [pid], reason="record spans multiple existing persons")
    for t, v in identifiers:
        db.add_identifier(con, pid, t, v)
    for i in range(len(identifiers)):
        for j in range(i + 1, len(identifiers)):
            db.add_edge(con, identifiers[i][0], identifiers[i][1], identifiers[j][0], identifiers[j][1],
                        reason="co-observed")
    return pid
