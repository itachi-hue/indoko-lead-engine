"""Sheet tab layout: column headers for each tab, in one place.

Hidden ID columns come first so rows are matched by ID, never by row number.
"""
from __future__ import annotations

REVIEW_COLUMNS = [
    "draft_id", "person_id", "Batch", "Group", "Name", "Title and company", "Evidence links",
    "Subject", "Body", "Follow-up 1", "Follow-up 2", "Check notes",
    "Decision", "Edited subject", "Edited body", "Edited follow-up 1", "Edited follow-up 2",
    "Reviewer", "Notes", "State",
]
DECISIONS = ["Approve", "Edit", "Reject"]

CRM_PIPELINE_COLUMNS = ["person_id", "Name", "Group", "Warm or cold", "Email", "Status",
                        "First sent", "Last event", "Positive reply date", "Reply reason"]
CRM_TEAM_COLUMNS = ["Booked", "Attended", "One-pager sent", "Client", "Owner", "Notes"]
CRM_COLUMNS = CRM_PIPELINE_COLUMNS + CRM_TEAM_COLUMNS

SUPPRESSION_COLUMNS = ["Email or domain", "Reason", "Added by", "Date"]

LINKEDIN_COLUMNS = ["person_id", "Name", "Title and company", "Profile", "Evidence",
                    "Suggested note", "Sent (yes/no)", "Accepted (yes/no)", "Notes"]

METRICS_COLUMNS = ["Week", "Group", "Warm or cold", "Delivered", "Bounces", "Bounce rate",
                   "Replies", "Positive replies", "Positive rate (95% range)", "Booked",
                   "Attended", "Clients", "Spend (USD)", "Cost per client", "Complaint rate (manual)"]


def col_letter(index: int) -> str:
    """0-based column index to A1 letter(s)."""
    s, n = "", index + 1
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def tabs(settings) -> dict[str, list[str]]:
    sh = settings.section("sheet")
    return {
        sh.get("review_tab", "Review"): REVIEW_COLUMNS,
        sh.get("crm_tab", "CRM"): CRM_COLUMNS,
        sh.get("suppression_tab", "Suppression"): SUPPRESSION_COLUMNS,
        sh.get("metrics_tab", "Metrics"): METRICS_COLUMNS,
        sh.get("linkedin_tab", "LinkedIn"): LINKEDIN_COLUMNS,
    }


def ensure_layout(ctx) -> None:
    """Create missing tabs, write headers, hide ID columns, add the Decision dropdown."""
    sheets = ctx.sheets
    layout = tabs(ctx.settings)
    sheets.ensure_tabs(list(layout))
    ids = sheets.tab_ids()
    sheets.batch_update_values([(f"{t}!A1:{col_letter(len(cols) - 1)}1", [cols]) for t, cols in layout.items()])
    reqs = []
    for title, cols in layout.items():
        sid = ids[title]
        reqs.append({"updateSheetProperties": {"properties": {"sheetId": sid,
                     "gridProperties": {"frozenRowCount": 1}}, "fields": "gridProperties.frozenRowCount"}})
        hidden = [i for i, c in enumerate(cols) if c in ("draft_id", "person_id")]
        for i in hidden:
            reqs.append({"updateDimensionProperties": {"range": {"sheetId": sid, "dimension": "COLUMNS",
                         "startIndex": i, "endIndex": i + 1}, "properties": {"hiddenByUser": True},
                         "fields": "hiddenByUser"}})
    review = ctx.settings.get("sheet.review_tab", "Review")
    dec = REVIEW_COLUMNS.index("Decision")
    reqs.append({"setDataValidation": {
        "range": {"sheetId": ids[review], "startRowIndex": 1, "startColumnIndex": dec, "endColumnIndex": dec + 1},
        "rule": {"condition": {"type": "ONE_OF_LIST", "values": [{"userEnteredValue": d} for d in DECISIONS]},
                 "strict": True, "showCustomUi": True}}})
    sheets.batch_update(reqs)
