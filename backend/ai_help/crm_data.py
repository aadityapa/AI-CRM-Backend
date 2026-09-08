"""Ask AI — live CRM data context (READ-ONLY).

Builds a compact, keyword-routed snapshot of the user's CRM data so the
assistant can answer questions like "top 5 candidates for REQ-2026-016",
"which POs expire this quarter" or "why is margin down on MARELLI".

Rules:
  * SELECT-only. Never mutates.
  * Finance figures (invoices, PO values) are included ONLY for users holding
    Finance / Sales_Head / Admin / CEO.
  * Every section is capped so the prompt stays small (~6 KB max).
"""
from __future__ import annotations

import re

from services.requirements import requirement_label
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

_MAX_CHARS = 6000

_FINANCE_ROLES = {"Finance", "Sales_Head", "Admin", "CEO"}


def _fin_ok(roles: set[str], is_admin: bool) -> bool:
    return is_admin or bool(roles & _FINANCE_ROLES)


def build_data_context(db: Session, roles: set[str], is_admin: bool, question: str) -> str:
    """Return a plain-text DATA CONTEXT block for the LLM (may be empty)."""
    from models import (
        AiInterviewLink, Candidate, Customer, Invoice, PurchaseOrder,
        Requirement, RequirementSkill, RequirementStatus, Resume, Skill,
    )

    q = (question or "").lower()
    today = date.today()
    lines: list[str] = [f"Today: {today.isoformat()}"]

    open_statuses = (
        RequirementStatus.OPEN_FOR_SOURCING, RequirementStatus.POSTED_ON_PORTALS,
        RequirementStatus.IN_PROGRESS,
    )

    # ---- Open requirements (always useful, cheap) -------------------------
    reqs = db.execute(
        select(Requirement).where(Requirement.status.in_(open_statuses))
        .order_by(Requirement.id.desc()).limit(15)
    ).scalars().all()
    if reqs:
        lines.append("\nOPEN REQUIREMENTS:")
        for r in reqs:
            skills = db.execute(
                select(Skill.name).join(RequirementSkill, RequirementSkill.skill_id == Skill.id)
                .where(RequirementSkill.requirement_id == r.id).limit(10)
            ).scalars().all()
            lines.append(
                f"- {requirement_label(r)} '{r.title}' [{getattr(r.status, 'value', r.status)}] "
                f"positions={r.no_of_positions} skills={', '.join(skills) or '—'}"
            )

    # ---- Candidates / resumes per requirement (when asked) ----------------
    if any(k in q for k in ("candidate", "resume", "req-", "req ", "opp-", "opp ", "match", "score", "shortlist")):
        wanted_ids: set[int] = set()
        for m in re.finditer(r"req[-\s]?(\d{4})[-\s]?(\d+)", q):
            num = f"REQ-{m.group(1)}-{m.group(2).zfill(3)}"
            r = db.execute(select(Requirement).where(Requirement.req_number == num)).scalars().first()
            if r:
                wanted_ids.add(r.id)
        # ONE id rule (18 Aug 2026): users quote OPP-2026-005, so resolve those too.
        for m in re.finditer(r"opp[-\s]?(\d{4})[-\s]?(\d+)", q):
            from models import Opportunity
            num = f"OPP-{m.group(1)}-{m.group(2).zfill(3)}"
            opp = db.execute(select(Opportunity).where(Opportunity.opp_id == num)).scalars().first()
            if opp:
                r = db.execute(
                    select(Requirement).where(Requirement.opportunity_id == opp.id)
                ).scalars().first()
                if r:
                    wanted_ids.add(r.id)
        if not wanted_ids:
            wanted_ids = {r.id for r in reqs[:3]}
        for rid in list(wanted_ids)[:3]:
            r = db.get(Requirement, rid)
            if r is None:
                continue
            rows = db.execute(
                select(Resume).where(Resume.requirement_id == rid)
                .order_by(Resume.ats_score.desc().nullslast()).limit(8)
            ).scalars().all()
            if rows:
                lines.append(f"\nCANDIDATES for {requirement_label(r)} '{r.title}' (by ATS score):")
                for x in rows:
                    ai_link = db.execute(
                        select(AiInterviewLink).where(AiInterviewLink.resume_id == x.id)
                        .order_by(AiInterviewLink.id.desc()).limit(1)
                    ).scalars().first()
                    ai_txt = (
                        f", AI interview {ai_link.result} {ai_link.overall_score_percent or ''}%"
                        if ai_link and ai_link.result != "Pending" else ""
                    )
                    lines.append(
                        f"- {x.candidate_name}: ATS {x.ats_score if x.ats_score is not None else 'not scanned'}"
                        f" [{getattr(x.ats_status, 'value', x.ats_status)}]{ai_txt}"
                    )

    # ---- PO expiry / values (finance-gated values) ------------------------
    if any(k in q for k in ("po", "purchase order", "expir", "renew")):
        pos = db.execute(
            select(PurchaseOrder, Customer.name)
            .join(Customer, Customer.id == PurchaseOrder.customer_id)
            .where(PurchaseOrder.end_date.isnot(None),
                   PurchaseOrder.end_date <= today + timedelta(days=180))
            .order_by(PurchaseOrder.end_date.asc()).limit(15)
        ).all()
        if pos:
            lines.append("\nPURCHASE ORDERS ending within 180 days:")
            for po, cust in pos:
                val = (
                    f" total={float(po.total_value):,.0f} balance={float(po.balance_value):,.0f}"
                    if _fin_ok(roles, is_admin) else ""
                )
                left = (po.end_date - today).days
                lines.append(f"- {po.po_number} ({cust}) ends {po.end_date} ({left}d){val}")

    # ---- Invoices / margin trend (finance-gated) --------------------------
    if _fin_ok(roles, is_admin) and any(k in q for k in ("invoice", "margin", "revenue", "billing", "paid", "outstanding")):
        # Customer names mentioned in the question → month-wise trend for them,
        # else overall by-customer totals for the last 6 months.
        cust_rows = db.execute(select(Customer.id, Customer.name).limit(200)).all()

        def _mentions(name: str) -> bool:
            m = re.match(r"[A-Za-z]{3,}", name or "")
            return bool(m and m.group(0).lower() in q)

        mentioned = [(cid, name) for cid, name in cust_rows if _mentions(name)]
        six_months_ago = today - timedelta(days=185)
        if mentioned:
            for cid, name in mentioned[:2]:
                rows = db.execute(
                    select(func.date_trunc("month", Invoice.invoice_date).label("m"),
                           func.sum(Invoice.grand_total), func.sum(Invoice.balance_amount))
                    .join(PurchaseOrder, PurchaseOrder.id == Invoice.po_id)
                    .where(PurchaseOrder.customer_id == cid, Invoice.invoice_date >= six_months_ago)
                    .group_by("m").order_by("m")
                ).all()
                lines.append(f"\nINVOICES for {name} (monthly, last 6 months):")
                for m, total, bal in rows:
                    lines.append(f"- {m.date().isoformat()[:7]}: billed={float(total or 0):,.0f} outstanding={float(bal or 0):,.0f}")
                if not rows:
                    lines.append("- no invoices in the last 6 months")
        else:
            rows = db.execute(
                select(Customer.name, func.sum(Invoice.grand_total), func.sum(Invoice.balance_amount))
                .join(PurchaseOrder, PurchaseOrder.id == Invoice.po_id)
                .join(Customer, Customer.id == PurchaseOrder.customer_id)
                .where(Invoice.invoice_date >= six_months_ago)
                .group_by(Customer.name).order_by(func.sum(Invoice.grand_total).desc()).limit(10)
            ).all()
            if rows:
                lines.append("\nINVOICED (last 6 months, by customer):")
                for name, total, bal in rows:
                    lines.append(f"- {name}: billed={float(total or 0):,.0f} outstanding={float(bal or 0):,.0f}")

    # ---- Bench / roll-offs ------------------------------------------------
    if any(k in q for k in ("bench", "roll", "redeploy", "free", "available engineer")):
        try:
            from services.dashboards import bench_rolloffs
            bench = bench_rolloffs(db, days=60)[:10]
            if bench:
                lines.append("\nBENCH (rolling off within 60 days):")
                for b in bench:
                    lines.append(
                        f"- {b['employee_name']} ({b['role_title'] or 'n/a'}) on {b['project_name']}"
                        f" — PO ends {b['po_end_date']} ({b['days_left']}d)"
                    )
        except Exception:
            pass

    # ---- Candidate total (cheap orientation) ------------------------------
    total_cands = db.execute(select(func.count()).select_from(Candidate)).scalar() or 0
    lines.append(f"\nTotals: candidates={total_cands}, open_requirements={len(reqs)}")

    text = "\n".join(lines)
    return text[:_MAX_CHARS]
