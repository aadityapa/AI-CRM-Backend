"""Expenses (4 Nov 2026) — Finance records every company expense; the CEO
dashboard turns them into net revenue (billed − expenses) and net profit.

Finance + Admin / CEO, by role (never template-widened): the figures sit beside
every customer's revenue on the CEO dashboard.
"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from crm_deps import CurrentUser, PageParams, get_crm_db, page_params, role_required
from models import Expense
from schemas.common import envelope
from services import expenses as svc
from services.crm_common import paginate, save_upload

router = APIRouter(prefix="/api/expenses", tags=["expenses"])

EXP_GATE = role_required("Finance")
PROOF_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".pdf", ".xlsx", ".xls", ".csv",
                    ".doc", ".docx")
PROOF_MAX_BYTES = 15 * 1024 * 1024
_SORTS = {"date": Expense.expense_date, "amount": Expense.amount, "category": Expense.category,
          "created": Expense.created_at}


class ExpenseIn(BaseModel):
    expense_date: date | None = None
    category: str | None = Field(default=None, max_length=64)
    description: str | None = Field(default=None, max_length=255)
    vendor: str | None = Field(default=None, max_length=255)
    amount: float | None = None
    tax_amount: float | None = None
    paid_via: str | None = Field(default=None, max_length=64)
    reference: str | None = Field(default=None, max_length=128)
    proof_url: str | None = Field(default=None, max_length=512)
    proof_name: str | None = Field(default=None, max_length=255)
    notes: str | None = Field(default=None, max_length=4000)
    branch_id: int | None = None


def _names(db: Session, rows) -> dict:
    from services.revenue_report import _user_names
    return _user_names(db, {r.created_by for r in rows if r.created_by})


def _filtered(date_from: date | None, date_to: date | None, category: str | None, search: str | None):
    q = select(Expense)
    if date_from:
        q = q.where(Expense.expense_date >= date_from)
    if date_to:
        q = q.where(Expense.expense_date <= date_to)
    if category:
        q = q.where(func.lower(Expense.category) == category.strip().lower())
    if search:
        like = f"%{search.strip()}%"
        q = q.where(or_(Expense.description.ilike(like), Expense.vendor.ilike(like),
                        Expense.reference.ilike(like), Expense.category.ilike(like),
                        Expense.notes.ilike(like)))
    return q


@router.get("/meta")
def expense_meta(db: Session = Depends(get_crm_db), user: CurrentUser = Depends(EXP_GATE)):
    return envelope({"categories": svc.category_list(db),
                     "paid_via": ["Bank transfer", "UPI", "Card", "Cheque", "Cash", "Other"]})


@router.post("/proof")
def upload_expense_proof(file: UploadFile = File(...), user: CurrentUser = Depends(EXP_GATE)):
    name = (file.filename or "proof").strip()
    if not name.lower().endswith(PROOF_EXTENSIONS):
        raise HTTPException(status_code=400, detail="Upload an image, PDF, Excel or Word file as the bill")
    size = getattr(file, "size", None)
    if size is not None and size > PROOF_MAX_BYTES:
        raise HTTPException(status_code=400, detail="The file is larger than 15 MB")
    return envelope({"url": save_upload(file, "expense_proofs"), "name": name[:255]},
                    message="Bill uploaded")


@router.get("")
def list_expenses(
    date_from: date | None = None, date_to: date | None = None,
    category: str | None = None,
    pp: PageParams = Depends(page_params),
    db: Session = Depends(get_crm_db), user: CurrentUser = Depends(EXP_GATE),
):
    if date_from and date_to and date_to < date_from:
        raise HTTPException(status_code=400, detail="'To' is before 'From'")
    q = _filtered(date_from, date_to, category, pp.search)
    col = _SORTS.get(pp.sort_by or "date", Expense.expense_date)
    q = q.order_by(col.asc() if pp.sort_dir == "asc" else col.desc(), Expense.id.desc())
    rows, page_meta = paginate(db, q, pp.page, pp.limit)
    names = _names(db, rows)
    # Totals over the WHOLE filtered set, not the page.
    sub = _filtered(date_from, date_to, category, pp.search).subquery()
    amount, tax, count = db.execute(
        select(func.coalesce(func.sum(sub.c.amount), 0), func.coalesce(func.sum(sub.c.tax_amount), 0),
               func.count(sub.c.id))).first()
    cats = db.execute(select(sub.c.category, func.sum(sub.c.amount)).group_by(sub.c.category)).all()
    by_cat = svc.by_category([{"category": c, "amount": float(a or 0), "date": None} for c, a in cats])
    return envelope([svc.expense_out(r, names) for r in rows],
                    meta={**page_meta,
                          "totals": {"amount": float(amount or 0), "tax_amount": float(tax or 0),
                                     "count": int(count or 0)},
                          "by_category": by_cat})


@router.post("")
def create_expense(body: ExpenseIn, db: Session = Depends(get_crm_db),
                   user: CurrentUser = Depends(EXP_GATE)):
    values = svc.clean(body.model_dump())
    e = Expense(**values, created_by=user.id)
    db.add(e)
    db.commit()
    db.refresh(e)
    return envelope(svc.expense_out(e, _names(db, [e])), message="Expense saved")


@router.put("/{expense_id}")
def update_expense(expense_id: int, body: ExpenseIn, db: Session = Depends(get_crm_db),
                   user: CurrentUser = Depends(EXP_GATE)):
    e = db.get(Expense, expense_id)
    if e is None:
        raise HTTPException(status_code=404, detail="Expense not found")
    values = svc.clean(body.model_dump(include=body.model_fields_set), partial=True)
    for k, v in values.items():
        setattr(e, k, v)
    db.commit()
    db.refresh(e)
    return envelope(svc.expense_out(e, _names(db, [e])), message="Expense updated")


@router.delete("/{expense_id}")
def delete_expense(expense_id: int, db: Session = Depends(get_crm_db),
                   user: CurrentUser = Depends(EXP_GATE)):
    e = db.get(Expense, expense_id)
    if e is None:
        raise HTTPException(status_code=404, detail="Expense not found")
    db.delete(e)
    db.commit()
    return envelope({"id": expense_id}, message="Expense deleted")


@router.get("/summary")
def expense_summary(month: str | None = Query(default=None), period: str | None = Query(default=None),
                    db: Session = Depends(get_crm_db), user: CurrentUser = Depends(EXP_GATE)):
    """Expenses month by month inside the period + by category (the tab's charts)."""
    from services.revenue_report import Period
    try:
        p = Period.parse(period or "fy", month, date.today())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    rows = svc.expenses_in(db, p.start, p.end)
    per_month = svc.by_month(rows)
    return envelope({
        "period": {"kind": p.kind, "key": p.key, "label": p.label,
                   "start": p.start.isoformat(), "end": p.end.isoformat()},
        "total": round(sum(r["amount"] for r in rows), 2),
        "monthly": [{"key": k, "label": p.first_month.shift(i).start.strftime("%b %y"),
                     "amount": round(per_month.get(k, 0.0), 2)}
                    for i, k in enumerate(p.month_keys)],
        "by_category": svc.by_category(rows),
    })
