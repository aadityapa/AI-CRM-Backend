"""The ONE builder of the AI interview "Full report" link (29 Sep 2026).

Six call sites used to format ``/admin/?view=candidateReport&cid=…&iid=…`` by
hand with the raw email — an address holding ``+`` (read as a space by the
dashboard's URLSearchParams) or ``&`` opened the wrong candidate or nothing.
The dashboard resolves the link through ``App.readInitialView`` →
``/hr/candidates/{email}/interviews/{record}``.
"""
from __future__ import annotations

from urllib.parse import urlencode


def ai_report_link(email: str | None, record_id) -> str | None:
    """The dashboard URL of one AI interview report, or None when either key is missing."""
    clean = (email or "").strip().lower()
    if not clean or record_id in (None, ""):
        return None
    return "/admin/?" + urlencode({"view": "candidateReport", "cid": clean, "iid": str(record_id)})
