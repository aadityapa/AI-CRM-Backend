"""CTC parsing and sanity limits — one place, because units are the bug.

2 Sep 2026, user bug report: a candidate showed **Current CTC ₹1,00,00,00,000**
(₹100 crore) on the Applicants tab. Cause: CTC is STORED IN RUPEES but several
UI fields are labelled "(Lac)" and multiply by 100,000 on save. Anything that
puts a rupee-scale number into a Lac-scale field is off by 10^5 — and nothing
validated the result, so the absurd value rendered as fact.

Resume text makes it worse: a CV may say "12 LPA", "12,00,000", "₹12L" or
"1.2 Cr" for the SAME salary. `parse_ctc_to_rupees` normalises all of those to
rupees so every caller stores the same scale.

Rules (deliberately conservative — a wrong guess is worse than no value):
  * an explicit unit always wins: lpa/lakh/lac/L → ×1e5, cr/crore → ×1e7,
    k/thousand → ×1e3;
  * with NO unit, magnitude decides: < 1,000 reads as lakhs (a CV writing
    "12" means 12 LPA, never ₹12), ≥ 1,000 reads as rupees already;
  * anything above MAX_CTC_RUPEES is rejected as unparseable rather than
    stored — that is exactly the ₹100-crore row this module exists for.
"""
from __future__ import annotations

import re

#: Hard ceiling for a single CTC figure. India's highest advertised salaries sit
#: far below this; a staffing CRM crossing it means a unit slip, not a raise.
MAX_CTC_RUPEES = 500_000_000.0  # ₹50 crore

#: Below this, a bare number is read as LAKHS rather than rupees.
_BARE_NUMBER_IS_LAKHS_BELOW = 1_000.0

_NUM_RE = re.compile(r"(\d+(?:[.,]\d+)*)")

#: A figure written with a lakh unit at or above this is rupees typed in a lakh box.
_RUPEES_IN_A_LAKH_BOX_FROM = 10_000.0


def lakhs_text(rupees) -> str | None:
    """PURE. ₹10,25,000 → "10.25 LPA" — how every screen prints a CTC."""
    try:
        num = float(rupees)
    except (TypeError, ValueError):
        return None
    if num <= 0:
        return None
    lakhs = round(num / 100_000, 2)
    text = f"{lakhs:.2f}".rstrip("0").rstrip(".")
    return f"{text} LPA"


def ctc_display(raw) -> str | None:
    """PURE. Whatever was typed ("700000 LPA", "10.2L", "₹10,00,000", "24.5") →
    "7 LPA" / "10.2 LPA" / "10 LPA" / "24.5 LPA". Unreadable text is kept as
    typed (a person may have written "negotiable"); blank → None."""
    text = str(raw or "").strip()
    if not text:
        return None
    rupees = parse_ctc_to_rupees(text)
    return lakhs_text(rupees) if rupees is not None else text


def repair_stored_rupees(value) -> float | None:
    """PURE. A STORED rupee figure above the ceiling is a lakh-box slip ×1e5:
    ₹70,00,00,00,000 → ₹7,00,000. Returns the repaired figure, or None when it
    does not need (or cannot take) a repair."""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if num <= MAX_CTC_RUPEES:
        return None
    fixed = num / 100_000
    if _RUPEES_IN_A_LAKH_BOX_FROM <= fixed <= MAX_CTC_RUPEES:
        return round(fixed, 2)
    return None


def parse_ctc_to_rupees(value) -> float | None:
    """Free-text CTC → rupees. None when it cannot be read confidently."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        num = float(value)
        text = ""
    else:
        text = str(value).strip().lower()
        if not text:
            return None
        m = _NUM_RE.search(text.replace(" ", ""))
        if not m:
            return None
        raw = m.group(1)
        # "12,00,000" / "1,200,000" → digits; "12.5" keeps its decimal point.
        if "." in raw and "," in raw:
            raw = raw.replace(",", "")
        elif "," in raw:
            raw = raw.replace(",", "")
        try:
            num = float(raw)
        except ValueError:
            return None

    if num <= 0:
        return None

    if re.search(r"\bcr\b|crore", text):
        num *= 10_000_000
    elif re.search(r"lpa|lakh|lacs?\b|\bl\b|\dl\b", text):
        # 6 Nov 2026 (user report): the careers form added " LPA" to whatever the
        # candidate typed, and people type rupees — "700000 LPA" read as ₹70,000
        # crore. A lakh figure of 10,000+ is a rupee figure typed in a lakh box;
        # 1,000–9,999 "LPA" (₹10–100 crore) cannot be read either way.
        if num >= _RUPEES_IN_A_LAKH_BOX_FROM:
            pass
        elif num >= _BARE_NUMBER_IS_LAKHS_BELOW:
            return None
        else:
            num *= 100_000
    elif re.search(r"\bk\b|thousand|\dk\b", text):
        num *= 1_000
    elif num < _BARE_NUMBER_IS_LAKHS_BELOW:
        # "12" on a CV means 12 LPA. Applies to numeric input too: nobody in
        # this system earns ₹12/year, so the reading is unambiguous.
        num *= 100_000

    if num > MAX_CTC_RUPEES:
        return None
    return round(num, 2)


def ctc_looks_wrong(value) -> str | None:
    """Reason string when a RUPEE figure is implausible, else None.

    Used to 400 a write instead of persisting a number the UI will render as
    ₹1,00,00,00,000. The message names the likely cause, because the cause is
    almost always a Lac-vs-rupee mix-up.
    """
    if value is None:
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return "must be a number"
    if num < 0:
        return "cannot be negative"
    if num > MAX_CTC_RUPEES:
        return (
            f"₹{num:,.0f} is not a realistic CTC — check the units. This value is "
            f"in rupees; a field labelled '(Lac)' expects 12 for ₹12,00,000, not "
            f"1200000. (Anything above ₹{MAX_CTC_RUPEES:,.0f} is refused.)"
        )
    return None
