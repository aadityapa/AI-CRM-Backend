"""What a typed search box really means (2 Nov 2026).

Reported with screenshots: an invoice number copied from a page or a mail
("KRNX26-27-01-CT)" — with a bracket or a full stop stuck to it) found nothing
anywhere. Every list's search goes through `crm_deps.page_params`, which now
runs `clean_search` — the ONE rule: spaces inside are collapsed, and brackets,
quotes and sentence punctuation wrapped around the term are dropped. Nothing
INSIDE the term is touched ("KRNX26-27-01-CT", "A&B Pvt. Ltd" stay as typed).

PURE — no I/O.
"""
from __future__ import annotations

import re

# Characters that only ever wrap a term (copied with it), never belong to it.
_WRAPPING = "()[]{}<>\"'`“”‘’.,;:!?|"
_SPACES = re.compile(r"\s+")


def clean_search(term: str | None) -> str | None:
    """The term a person meant to search for, or None when nothing is left."""
    if term is None:
        return None
    text = _SPACES.sub(" ", str(term)).strip()
    prev = None
    while text and text != prev:
        prev = text
        text = text.strip().strip(_WRAPPING).strip()
    return text or None
