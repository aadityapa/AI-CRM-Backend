"""Company expenses + net revenue (4 Nov 2026).

User ask: "in the Finance login add an Expense tab for all expenses; calculate
net revenue and show it on the CEO / Admin dashboard monthly, quarterly, yearly."
User decision: **net revenue = billed − expenses**; net profit (billed − people
cost − expenses) is shown beside it.

Amounts are BEFORE GST (`amount`); the GST paid sits in `tax_amount` and is
never counted as an expense (it is claimed back as input credit).
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import Decimal, InvalidOperation

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

#: Suggested categories (free text is allowed; these fill the picker).
CATEGORIES = (
    "Rent & office", "Salaries (non-billable staff)", "Software & subscriptions",
    "Hardware & equipment", "Travel", "Recruitment & job portals", "Marketing",
    "Professional fees", "Utilities & internet", "Training", "Statutory & taxes",
    "Bank charges", "Other",
)
MAX_AMOUNT = Decimal("1000000000")  # ₹100 Cr — a typo guard


def _money(raw, field: str) -> Decimal:
    try:
        v = Decimal(str(raw if raw not in (None, "") else "0")).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        raise HTTPException(status_code=400, detail=f"{field} must be a number")
    if v < 0 or v > MAX_AMOUNT:
        raise HTTPException(status_code=400, detail=f"{field} must be between 0 and ₹100 Cr")
    return v


def clean(values: dict, partial: bool = False) -> dict:
    """Validate an expense body. `partial` = an edit (only the keys sent)."""
    out = {}
    if not partial or "expense_date" in values:
        d = values.get("expense_date")
        if not d:
            raise HTTPException(status_code=400, detail="Pick the expense date")
        if isinstance(d, str):
            try:
                d = date.fromisoformat(d)
            except ValueError:
                raise HTTPException(status_code=400, detail="Expense date must be YYYY-MM-DD")
        if d > date.today():
            raise HTTPException(status_code=400, detail="The expense date is in the future")
        out["expense_date"] = d
    for key, need, cap in (("category", True, 64), ("description", True, 255)):
        if not partial or key in values:
            v = (values.get(key) or "").strip()
            if need and not v:
                raise HTTPException(status_code=400, detail=f"Enter the {key}")
            out[key] = v[:cap]
    if not partial or "amount" in values:
        amt = _money(values.get("amount"), "Amount")
        if amt <= 0:
            raise HTTPException(status_code=400, detail="Amount must be more than zero")
        out["amount"] = amt
    if not partial or "tax_amount" in values:
        out["tax_amount"] = _money(values.get("tax_amount"), "GST")
    for key, cap in (("vendor", 255), ("paid_via", 64), ("reference", 128), ("proof_url", 512),
                     ("proof_name", 255), ("notes", 4000)):
        if key in values:
            v = (values.get(key) or "").strip()
            out[key] = v[:cap] or None
    if "proof_url" in out and out["proof_url"] and not out["proof_url"].startswith("/api/crm-files/"):
        raise HTTPException(status_code=400, detail="Attach the proof through the upload box")
    if "branch_id" in values:
        out["branch_id"] = values.get("branch_id") or None
    return out


def expense_out(e, names: dict | None = None) -> dict:
    return {
        "id": e.id,
        "expense_date": e.expense_date.isoformat() if e.expense_date else None,
        "category": e.category, "description": e.description, "vendor": e.vendor,
        "amount": float(e.amount or 0), "tax_amount": float(e.tax_amount or 0),
        "total": float((e.amount or 0) + (e.tax_amount or 0)),
        "paid_via": e.paid_via, "reference": e.reference,
        "proof_url": e.proof_url, "proof_name": e.proof_name, "notes": e.notes,
        "branch_id": e.branch_id,
        "created_by": e.created_by, "created_by_name": (names or {}).get(e.created_by),
        "created_at": e.created_at.isoformat() if e.created_at else None,
    }


def expenses_in(db: Session, start: date, end: date) -> list[dict]:
    """[{date, category, amount}] in a window — the dashboards' input."""
    from models import Expense
    rows = db.execute(select(Expense.expense_date, Expense.category, Expense.amount)
                      .where(Expense.expense_date >= start, Expense.expense_date <= end)).all()
    return [{"date": d, "category": c, "amount": float(a or 0)} for d, c, a in rows]


def by_month(rows: list[dict]) -> dict[str, float]:
    """"YYYY-MM" → total. PURE."""
    out: dict[str, float] = defaultdict(float)
    for r in rows:
        if r["date"]:
            out[f"{r['date'].year:04d}-{r['date'].month:02d}"] += r["amount"]
    return dict(out)


def by_category(rows: list[dict]) -> list[dict]:
    """Largest first. PURE."""
    out: dict[str, float] = defaultdict(float)
    for r in rows:
        out[r["category"] or "Other"] += r["amount"]
    total = sum(out.values()) or 0.0
    return [{"category": k, "amount": round(v, 2),
             "share_pct": round(v / total * 100, 1) if total else 0.0}
            for k, v in sorted(out.items(), key=lambda kv: -kv[1])]


def net_figures(billed: float, people_cost: float, expenses: float) -> dict:
    """Net revenue = billed − expenses; net profit also takes off people cost. PURE."""
    net_revenue = billed - expenses
    net_profit = billed - people_cost - expenses
    return {
        "billed": round(billed, 2), "expenses": round(expenses, 2),
        "people_cost": round(people_cost, 2),
        "net_revenue": round(net_revenue, 2),
        "net_profit": round(net_profit, 2),
        "net_margin_pct": round(net_profit / billed * 100, 1) if billed else None,
        "expense_ratio_pct": round(expenses / billed * 100, 1) if billed else None,
    }


def category_list(db: Session) -> list[str]:
    """Suggested categories + every one already used."""
    from models import Expense
    used = [c for (c,) in db.execute(select(Expense.category).group_by(Expense.category)
                                      .order_by(func.count().desc())).all() if c]
    seen, out = set(), []
    for c in list(CATEGORIES) + used:
        if c.lower() not in seen:
            seen.add(c.lower())
            out.append(c)
    return out
