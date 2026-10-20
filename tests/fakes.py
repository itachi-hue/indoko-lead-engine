"""In-memory fakes for the Sheet, Gemini and Instantly. The real clients' request shapes are
tested separately against the HTTP router."""
from __future__ import annotations

import re

from lead_engine.http import HTTPError


def _a1(rng: str) -> tuple[str, int, int, int | None]:
    """'Tab!B3:D3' -> (tab, row, start_col, end_col); 'Tab' -> (tab, 0, 0, None)."""
    tab, _, cells = rng.partition("!")
    tab = tab.strip("'")
    if not cells:
        return tab, 0, 0, None
    m = re.match(r"([A-Z]+)(\d+)(?::([A-Z]+)(\d+))?", cells)

    def col(s):
        n = 0
        for ch in s:
            n = n * 26 + ord(ch) - 64
        return n - 1
    start = col(m.group(1))
    end = col(m.group(3)) if m.group(3) else start
    return tab, int(m.group(2)), start, end


class FakeSheets:
    def __init__(self):
        self.tabs: dict[str, list[list[str]]] = {}
        self.requests: list[dict] = []

    def tab_ids(self):
        return {t: i for i, t in enumerate(self.tabs)}

    def ensure_tabs(self, titles):
        for t in titles:
            self.tabs.setdefault(t, [])

    def metadata(self):
        return {"properties": {"title": "Fake"}, "sheets": [{"properties": {"title": t, "sheetId": i}}
                                                             for t, i in self.tab_ids().items()]}

    def batch_update(self, requests):
        self.requests.extend(requests)
        return {}

    def get_values(self, tab):
        return [list(r) for r in self.tabs.get(tab, [])]

    def _set(self, rng, rows):
        tab, row, start, _ = _a1(rng)
        grid = self.tabs.setdefault(tab, [])
        for i, values in enumerate(rows):
            r = row - 1 + i
            while len(grid) <= r:
                grid.append([])
            line = grid[r]
            while len(line) < start + len(values):
                line.append("")
            for j, v in enumerate(values):
                line[start + j] = "" if v is None else str(v)

    def update_values(self, rng, rows):
        self._set(rng, rows)

    def batch_update_values(self, data):
        for rng, rows in data:
            self._set(rng, rows)

    def append_rows(self, tab, rows):
        grid = self.tabs.setdefault(tab, [])
        for r in rows:
            grid.append(["" if v is None else str(v) for v in r])

    # helpers for tests
    def header(self, tab):
        return self.tabs[tab][0]

    def row_by_id(self, tab, id_):
        for r in self.tabs.get(tab, [])[1:]:
            if id_ in r[:2]:
                return r
        return None

    def set_cell(self, tab, id_, column, value):
        header = self.tabs[tab][0]
        ci = header.index(column)
        for r in self.tabs[tab][1:]:
            if id_ in r[:2]:
                while len(r) <= ci:
                    r.append("")
                r[ci] = value
                return
        raise KeyError(id_)


class FakeGemini:
    """Returns queued outputs in order; an Exception instance in the queue is raised."""

    def __init__(self, outputs=None):
        self.outputs = list(outputs or [])
        self.prompts: list[str] = []

    def generate_json(self, system, prompt, schema, temperature=0.6):
        self.prompts.append(prompt)
        out = self.outputs.pop(0)
        if isinstance(out, Exception):
            raise out
        return out


class FakeInstantly:
    def __init__(self, api_ok=True):
        self.api_ok = api_ok
        self.leads: dict[str, dict] = {}       # lead_id -> lead
        self.added: list[dict] = []
        self.blocked: list[str] = []
        self.deleted: list[str] = []
        self.paused: list[str] = []
        self.received: list[dict] = []
        self.fail_add_with: int | None = None
        self.skip_emails: set[str] = set()
        self._n = 0

    def ping(self):
        return {"ok": self.api_ok}

    def add_lead(self, campaign_id, email, first_name, custom_variables):
        if self.fail_add_with:
            raise HTTPError(self.fail_add_with, "fail")
        if email in self.skip_emails:
            return {"status": "skipped"}
        self._n += 1
        lid = f"lead{self._n}"
        lead = {"id": lid, "campaign": campaign_id, "email": email, "first_name": first_name,
                "custom_variables": custom_variables, "status": 1}
        self.leads[lid] = lead
        self.added.append(lead)
        return lead

    def find_lead(self, campaign_id, email):
        for lead in self.leads.values():
            if lead["campaign"] == campaign_id and lead["email"].lower() == email.lower():
                return lead
        return None

    def list_leads(self, campaign_id):
        return [dict(l) for l in self.leads.values() if l["campaign"] == campaign_id]

    def list_received_emails(self, since_iso=None):
        return [e for e in self.received if not since_iso or e["timestamp_created"] > since_iso]

    def delete_lead(self, lead_id):
        self.deleted.append(lead_id)
        self.leads.pop(lead_id, None)

    def block(self, value):
        self.blocked.append(value)

    def pause_campaign(self, campaign_id):
        self.paused.append(campaign_id)


def good_output(evidence_ids=(), subject="Your seed round", body=None, fu1=None, fu2=None):
    return {
        "subject": subject,
        "body": body or ("Hi Ada,\n\nSaw the news about the Northwind seed round. Some founders look at "
                         "long-term US options such as the O-1, EB-1A or EB-2 NIW as the company grows. "
                         "Would a 30-minute consultation help? https://cal.com/indoko/founder\n\nKarishma"),
        "fu1": fu1 or "Hi Ada, a short note in case the timing is better now. Happy to answer questions by email too.",
        "fu2": fu2 or "Hi Ada, last note from me. If US options come up later, we're glad to help.",
        "evidence_ids": list(evidence_ids),
    }
