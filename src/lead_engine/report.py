"""Weekly funnel metrics for the Metrics tab (docs/DESIGN.md sections 18.3 and 18.4).

Rows are keyed by (week, group, warm or cold). The complaint-rate column is entered by hand from
Google Postmaster Tools, so it is never overwritten.
"""
from __future__ import annotations

import datetime as dt
import math
from collections import defaultdict

from .sheets.layout import METRICS_COLUMNS, col_letter

FUNNEL = ("sent", "bounce", "reply", "positive_reply", "booked", "attended", "client")


def wilson(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson score interval for a proportion, as fractions."""
    if n <= 0:
        return 0.0, 0.0
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def week_of(ts: str) -> str:
    d = dt.date.fromisoformat(ts[:10])
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def compute(con) -> list[dict]:
    """One dict per (week, group, source). Week = week of the first send to that person."""
    first_sent = {r["person_id"]: r["t"] for r in con.execute(
        "SELECT d.person_id, MIN(s.sent_at) t FROM sends s JOIN drafts d ON d.draft_id=s.draft_id "
        "WHERE s.status IN ('sent','exported') GROUP BY d.person_id")}
    people = {r["person_id"]: r for r in con.execute(
        f"SELECT person_id, segment, group_source FROM persons WHERE person_id IN "
        f"({','.join('?' * len(first_sent))})", tuple(first_sent))} if first_sent else {}
    counts: dict[tuple, dict] = defaultdict(lambda: {k: 0 for k in FUNNEL})
    seen: set[tuple] = set()
    for e in con.execute("SELECT person_id, type FROM events"):
        pid = e["person_id"]
        if pid not in first_sent or e["type"] not in FUNNEL or (pid, e["type"]) in seen:
            continue
        seen.add((pid, e["type"]))  # count each person once per stage
        p = people[pid]
        key = (week_of(first_sent[pid]), p["segment"], p["group_source"] or "cold")
        counts[key][e["type"]] += 1
    spend = defaultdict(float)
    for r in con.execute("SELECT started_at, cost_usd FROM runs WHERE cost_usd > 0"):
        spend[week_of(r["started_at"])] += r["cost_usd"]
    rows = []
    for (week, seg, src), c in sorted(counts.items()):
        delivered = max(c["sent"] - c["bounce"], 0)
        lo, hi = wilson(c["positive_reply"], delivered)
        rows.append({"week": week, "group": seg, "source": src, **c, "delivered": delivered,
                     "bounce_rate": (c["bounce"] / c["sent"]) if c["sent"] else 0.0,
                     "positive_rate": (c["positive_reply"] / delivered) if delivered else 0.0,
                     "positive_lo": lo, "positive_hi": hi, "spend": round(spend.get(week, 0.0), 2)})
    # spend is per week; attribute it to that week's rows in proportion to sends
    by_week = defaultdict(int)
    for r in rows:
        by_week[r["week"]] += r["sent"]
    for r in rows:
        total = by_week[r["week"]]
        r["spend"] = round(r["spend"] * (r["sent"] / total), 2) if total else 0.0
        r["cost_per_client"] = round(r["spend"] / r["client"], 2) if r["client"] else None
    return rows


def as_cells(r: dict) -> list:
    pct = lambda x: f"{100 * x:.1f}%"  # noqa: E731
    return [r["week"], r["group"], r["source"], r["delivered"], r["bounce"], pct(r["bounce_rate"]), r["reply"],
            r["positive_reply"], f"{pct(r['positive_rate'])} ({pct(r['positive_lo'])}–{pct(r['positive_hi'])})",
            r["booked"], r["attended"], r["client"], r["spend"],
            "" if r["cost_per_client"] is None else r["cost_per_client"]]


def write(ctx, run) -> int:
    rows = compute(ctx.con)
    tab = ctx.settings.get("sheet.metrics_tab", "Metrics")
    values = ctx.sheets.get_values(tab)
    existing = {}
    for i, r in enumerate(values[1:] if values else []):
        if len(r) >= 3:
            existing[(r[0], r[1], r[2])] = i + 2
    last = col_letter(len(METRICS_COLUMNS) - 2)   # stop before the manual complaint-rate column
    updates, appends = [], []
    for r in rows:
        cells = as_cells(r)
        key = (r["week"], r["group"], r["source"])
        if key in existing:
            n = existing[key]
            updates.append((f"{tab}!A{n}:{last}{n}", [cells]))
        else:
            appends.append(cells + [""])
    if ctx.dry_run:
        print(f"[dry-run] metrics: {len(updates)} rows updated, {len(appends)} added")
        return len(rows)
    ctx.sheets.batch_update_values(updates)
    ctx.sheets.append_rows(tab, appends)
    run.ok += len(rows)
    return len(rows)


def reply_reasons(con) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for r in con.execute("SELECT c.cursor reason, p.segment FROM crawl_cursors c JOIN persons p "
                         "ON p.person_id=c.ckey WHERE c.source='reply_reason'"):
        out[r["segment"]][r["reason"]] += 1
    return {k: dict(v) for k, v in out.items()}
