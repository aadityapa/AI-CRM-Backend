"""Where every invoice's money stands, split into bank and TDS (6 Nov 2026).

User ask: the CEO Finance tab's "How the year unfolded" reads in these words —
Revenue (Ex. Tax) · Revenue (Inc Tax) · Receivable Bank · Receivable TDS ·
Received Bank · Received TDS · Due Bank · Due TDS · Total Due · Predicted
revenue (project count).

The rule, per Tax invoice, counted in the invoice's WORK month (the dashboard's
month rule — `revenue_report.work_date`):

* Revenue (Ex. Tax)  = sub_total (value before GST).
* Revenue (Inc Tax)  = grand_total (what the invoice asks for, GST included).
* Receivable TDS     = the TDS the customer deducted, as booked on the invoice
                       (`TdsRecord.tds_amount`). An invoice still OPEN with no TDS
                       booked yet is expected to have TDS deducted at Settings ▸
                       `finance.tds_rate_percent` (default 10 %) on the value before
                       GST — never more than what is still unpaid. A fully paid
                       invoice with no TDS booked had none.
* Receivable Bank    = Revenue (Inc Tax) − Receivable TDS (the cash the customer
                       should send).
* Received Bank      = cash recorded against the invoice (InvoicePayment).
* Received TDS       = TDS the customer has deposited and Finance marked received
                       (Form 16A — `TdsRecord.tds_paid`).
* Due Bank           = Receivable Bank − Received Bank, never below 0 and never
                       above the invoice's open balance (a credit note lowers it).
* Due TDS            = Receivable TDS − Received TDS ("pending from client").
* Total Due          = Due Bank + Due TDS.

20 Nov 2026 (CEO ask: "after paid / partially paid the dashboard should move the
same as Revenue (Ex. Tax)") — two words on the before-GST basis, so they always add
up to Revenue (Ex. Tax):

* Due (Ex. Tax)      = the invoice's open balance × (sub_total ÷ grand_total);
                       0 once the invoice reads Paid (cash, TDS deducted, a
                       settlement — whatever closed it).
* Received (Ex. Tax) = Revenue (Ex. Tax) − Due (Ex. Tax). A fully paid invoice
                       counts its whole value before GST; a partly paid one its share.

Everything except `tds_by_invoice` is PURE.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

KEYS = ("revenue_ex", "revenue_inc", "receivable_bank", "receivable_tds", "received_bank",
        "received_tds", "due_bank", "due_tds", "total_due", "expected_tds",
        "received_ex", "due_ex")


def ex_tax_split(sub_total: float, grand_total: float, balance: float, paid_in_full: bool) -> tuple[float, float]:
    """(received_ex, due_ex) — the invoice's value before GST split into what is
    settled and what is still open; the two always add up to sub_total. PURE."""
    sub, grand = max(0.0, _f(sub_total)), max(0.0, _f(grand_total))
    if paid_in_full or sub <= 0:
        return round(sub, 2), 0.0
    share = (sub / grand) if grand > 0 else 1.0
    due = round(min(sub, max(0.0, _f(balance)) * share), 2)
    return round(sub - due, 2), due


def _f(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def expected_tds_rate() -> float:
    """Settings ▸ Finance ▸ TDS rate (%) as a fraction. Never raises."""
    try:
        from services.org_settings import setting

        pct = float(setting("finance.tds_rate_percent") or 10)
    except Exception:  # noqa: BLE001
        pct = 10.0
    return max(0.0, min(pct, 30.0)) / 100.0


def invoice_money(inv: dict, cash: float, tds: dict | None, rate: float) -> dict:
    """One invoice's split. `tds` = {"amount": booked, "received": Form 16A in}. PURE."""
    ex, inc = _f(inv.get("sub_total")), _f(inv.get("grand_total"))
    balance = max(0.0, _f(inv.get("balance")))
    paid_in_full = str(inv.get("status") or "").lower() == "paid"
    booked = _f((tds or {}).get("amount"))
    tds_in = _f((tds or {}).get("received"))
    expected = 0.0
    if booked > 0:
        rec_tds = booked
    elif not paid_in_full and balance > 0.5:
        expected = min(round(ex * rate, 2), max(0.0, inc - cash))
        rec_tds = expected
    else:
        rec_tds = 0.0
    rec_bank = max(0.0, inc - rec_tds)
    due_bank = max(0.0, min(rec_bank - cash, balance if booked > 0 or paid_in_full else balance - expected))
    if paid_in_full:
        due_bank = 0.0
    due_tds = max(0.0, rec_tds - tds_in)
    received_ex, due_ex = ex_tax_split(ex, inc, balance, paid_in_full)
    return {
        "received_ex": received_ex, "due_ex": due_ex,
        "revenue_ex": ex, "revenue_inc": inc,
        "receivable_bank": rec_bank, "receivable_tds": rec_tds,
        "received_bank": cash, "received_tds": min(tds_in, rec_tds),
        "due_bank": due_bank, "due_tds": due_tds, "total_due": due_bank + due_tds,
        "expected_tds": expected,
    }


def money_by_month(months, invoices: list[dict], payments: list[dict],
                   tds: dict[int, dict], rate: float) -> dict[str, dict]:
    """{month key: {KEYS…}} over the invoices counted in each month. PURE.

    `payments` are the rows `revenue_report._payments_in_window` returns (mapped
    cash only — money still on account is not received against any invoice)."""
    cash_by: dict[int, float] = defaultdict(float)
    for p in payments:
        if p.get("invoice_id") and not p.get("on_account"):
            cash_by[p["invoice_id"]] += _f(p.get("amount"))
    out: dict[str, dict] = {}
    for m in months:
        acc = {k: 0.0 for k in KEYS}
        n = 0
        for inv in invoices:
            d = inv.get("invoice_date")
            if not d or not (m.start <= d <= m.end):
                continue
            n += 1
            row = invoice_money(inv, cash_by.get(inv["id"], 0.0), tds.get(inv["id"]), rate)
            for k in KEYS:
                acc[k] += row[k]
        out[m.key] = {**{k: round(v, 2) for k, v in acc.items()}, "invoices": n}
    return out


def totals(rows: list[dict]) -> dict:
    """Sum of the month rows' money keys. PURE."""
    return {k: round(sum(_f(r.get(k)) for r in rows), 2) for k in KEYS}


def tds_by_invoice(db: Session, invoice_ids: list[int]) -> dict[int, dict]:
    """{invoice id: {amount, received}} from TdsRecord. Never raises."""
    if not invoice_ids:
        return {}
    from models import TdsRecord

    out: dict[int, dict] = {}
    try:
        with db.begin_nested():
            for iid, amount, paid in db.execute(
                    select(TdsRecord.invoice_id, TdsRecord.tds_amount, TdsRecord.tds_paid)
                    .where(TdsRecord.invoice_id.in_(list(set(invoice_ids))))).all():
                out[iid] = {"amount": _f(amount), "received": _f(paid)}
    except Exception:  # noqa: BLE001
        return {}
    return out


def comparison_partial(previous_start: date) -> bool:
    """True when the comparison period starts before the work month Orbit has
    data from (Settings ▸ Billing ▸ Data starts from) — "+525 % vs last FY"
    then compares a full year with a few months. Never raises."""
    try:
        from services.billing_rules import data_start_date

        return previous_start < data_start_date()
    except Exception:  # noqa: BLE001
        return False
