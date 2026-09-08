"""Shared cascade-delete helpers for CRM list row deletes.

Finance-safe rules:
- Invoice payments and TDS payments always block hard delete.
- Credit notes always block.
- Unpaid TDS with no payments cascades with its invoice.
- Draft / non-approved timesheets cascade; approved sheets cascade only after
  their linked invoices (if any) have been removed.
"""
from __future__ import annotations

from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from models import (
    AiInterviewLink,
    CandidateProfile,
    CreditNote,
    CustomerLeavePolicy,
    Employee,
    Holiday,
    InterviewEvent,
    InterviewSlot,
    Invoice,
    LeaveApplication,
    OpportunityAttachment,
    POProjectAllocation,
    ProjectEmployeeLeaveDetail,
    PurchaseOrder,
    Requirement,
    SlotBooking,
    TemplateRequest,
    Timesheet,
    TimesheetActivityLog,
    TimesheetStatus,
)
from services import tax


def _ev(v) -> str:
    return v.value if hasattr(v, "value") else str(v)


def invoice_hard_delete_blockers(invoice: Invoice) -> list[str]:
    """Human-readable blockers that prevent cascading an invoice."""
    blockers: list[str] = []
    num = invoice.invoice_number or f"#{invoice.id}"
    if invoice.payments:
        blockers.append(f"{num} has recorded payments")
    tds = invoice.tds_record
    if tds is not None:
        paid = Decimal(str(tds.tds_paid or 0))
        pays = list(tds.payments or [])
        if paid > 0 or pays:
            blockers.append(f"{num} has TDS payment(s)")
    return blockers


def credit_note_count(db: Session, invoice_id: int) -> int:
    return db.execute(
        select(func.count()).select_from(CreditNote).where(CreditNote.invoice_id == invoice_id)
    ).scalar() or 0


def assert_invoice_cascade_ok(db: Session, invoice: Invoice) -> None:
    blockers = invoice_hard_delete_blockers(invoice)
    cn = credit_note_count(db, invoice.id)
    if cn:
        blockers.append(f"{invoice.invoice_number or invoice.id} has {cn} credit note(s)")
    if blockers:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot delete: {'; '.join(blockers)}. Reverse them first.",
        )


def cascade_delete_invoice(db: Session, invoice: Invoice) -> None:
    """Delete invoice + owned unpaid TDS; reverse PO consumption. No commit."""
    assert_invoice_cascade_ok(db, invoice)
    if invoice.po_id is not None:
        po = db.get(PurchaseOrder, invoice.po_id)
        if po is not None:
            tax.apply_po_consumption(po, -Decimal(str(invoice.grand_total)))
            alloc = db.execute(
                select(POProjectAllocation).where(
                    POProjectAllocation.po_id == po.id,
                    POProjectAllocation.project_id == invoice.project_id,
                )
            ).scalar_one_or_none()
            if alloc is not None:
                alloc.consumed_amount = max(
                    Decimal(str(alloc.consumed_amount)) - Decimal(str(invoice.grand_total)),
                    Decimal("0"),
                )
    # ORM cascade deletes owned unpaid TDS (payments already blocked above).
    db.delete(invoice)


def undo_invoice_admin(db: Session, invoice: Invoice) -> dict:
    """Admin/CEO-only UNDO of an invoice raised by mistake (3 Sep 2026, user
    decision: "Admin/CEO can undo any change").

    Unlike `cascade_delete_invoice`, recorded payments and TDS payments do NOT
    block — the invoice was wrong, so whatever was booked against it is wrong
    too, and it goes with the invoice (ORM cascade). Credit notes STILL block:
    a credit note means the invoice reached the customer and was corrected
    the proper way; unwinding underneath it would leave a dangling document.
    PO consumption is reversed exactly as the normal delete does.

    Returns what was reversed, for the activity log. No commit.
    """
    num = invoice.invoice_number or f"#{invoice.id}"
    cn = credit_note_count(db, invoice.id)
    if cn:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot undo: {num} has {cn} credit note(s) issued against it. "
                   "It has already been corrected with the customer — reverse that first.",
        )
    payments = list(invoice.payments or [])
    paid_total = sum((Decimal(str(p.amount or 0)) for p in payments), Decimal("0"))
    tds = invoice.tds_record
    tds_paid = Decimal(str(tds.tds_paid or 0)) if tds is not None else Decimal("0")
    summary = {
        "invoice_number": num,
        "grand_total": float(invoice.grand_total or 0),
        "payments_reversed": len(payments),
        "payments_amount": float(paid_total),
        "tds_paid_reversed": float(tds_paid),
    }
    if invoice.po_id is not None:
        po = db.get(PurchaseOrder, invoice.po_id)
        if po is not None:
            tax.apply_po_consumption(po, -Decimal(str(invoice.grand_total)))
            alloc = db.execute(
                select(POProjectAllocation).where(
                    POProjectAllocation.po_id == po.id,
                    POProjectAllocation.project_id == invoice.project_id,
                )
            ).scalar_one_or_none()
            if alloc is not None:
                alloc.consumed_amount = max(
                    Decimal(str(alloc.consumed_amount)) - Decimal(str(invoice.grand_total)),
                    Decimal("0"),
                )
    db.delete(invoice)  # lines, payments, TDS record + TDS payments cascade
    return summary


def cascade_delete_invoices(db: Session, invoices: list[Invoice]) -> None:
    """Cascade many invoices; raises 409 listing all hard blockers."""
    blockers: list[str] = []
    for inv in invoices:
        blockers.extend(invoice_hard_delete_blockers(inv))
        cn = credit_note_count(db, inv.id)
        if cn:
            blockers.append(f"{inv.invoice_number or inv.id} has {cn} credit note(s)")
    if blockers:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot delete: {'; '.join(blockers)}. Reverse them first.",
        )
    for inv in invoices:
        cascade_delete_invoice(db, inv)


def purge_timesheet(db: Session, ts: Timesheet) -> None:
    """Hard-delete a timesheet and its activity log (entries cascade via ORM)."""
    for log in db.execute(
        select(TimesheetActivityLog).where(TimesheetActivityLog.timesheet_id == ts.id)
    ).scalars().all():
        db.delete(log)
    db.delete(ts)


def cascade_project_timesheets(db: Session, project_id: int, *, include_approved: bool = False) -> None:
    """Delete timesheets on a project. Approved sheets only when include_approved."""
    q = select(Timesheet).where(Timesheet.project_id == project_id)
    if not include_approved:
        q = q.where(Timesheet.status != TimesheetStatus.APPROVED)
    for ts in db.execute(q).scalars().all():
        purge_timesheet(db, ts)


def pe_timesheet_query(pe_id: int, project_id: int, employee_id: int):
    return select(Timesheet).where(
        or_(
            Timesheet.project_employee_id == pe_id,
            (Timesheet.project_id == project_id) & (Timesheet.employee_id == employee_id),
        )
    )


def cascade_pe_draft_timesheets(db: Session, pe_id: int, project_id: int, employee_id: int) -> None:
    """Delete non-approved timesheets for a project-employee assignment."""
    rows = db.execute(pe_timesheet_query(pe_id, project_id, employee_id)).scalars().all()
    for ts in rows:
        if _ev(ts.status) == TimesheetStatus.APPROVED.value:
            continue
        purge_timesheet(db, ts)


def invoices_for_timesheet_ids(db: Session, ts_ids: list[int]) -> list[Invoice]:
    if not ts_ids:
        return []
    return list(
        db.execute(select(Invoice).where(Invoice.timesheet_id.in_(ts_ids))).scalars().all()
    )


def invoices_for_project(db: Session, project_id: int) -> list[Invoice]:
    return list(
        db.execute(select(Invoice).where(Invoice.project_id == project_id)).scalars().all()
    )


def purge_requirement_owned(db: Session, requirement_id: int) -> None:
    """Delete scheduling + template-request children of a requirement (FK order)."""
    for book in db.execute(
        select(SlotBooking).where(SlotBooking.requirement_id == requirement_id)
    ).scalars().all():
        db.delete(book)
    for slot in db.execute(
        select(InterviewSlot).where(InterviewSlot.requirement_id == requirement_id)
    ).scalars().all():
        db.delete(slot)
    for tr in db.execute(
        select(TemplateRequest).where(TemplateRequest.requirement_id == requirement_id)
    ).scalars().all():
        db.delete(tr)


def purge_profile_interviews(db: Session, profile_id: int) -> None:
    """Delete interview rounds owned by a candidate profile.

    ``interview_events.profile_id`` is NOT NULL with no DB-level cascade, so a
    profile that ever had an L2 face-to-face (or an imported interview round)
    cannot be deleted until these rows go — otherwise the delete fails with an
    IntegrityError and the API returns a misleading 409.
    """
    for ev in db.execute(
        select(InterviewEvent).where(InterviewEvent.profile_id == profile_id)
    ).scalars().all():
        db.delete(ev)
    db.flush()


def cascade_opportunity_children(db: Session, opportunity_id: int) -> None:
    """Cascade opp-owned recruiting children. Caller must block if projects exist."""
    for link in db.execute(
        select(AiInterviewLink).where(AiInterviewLink.opportunity_id == opportunity_id)
    ).scalars().all():
        db.delete(link)
    db.flush()
    for profile in db.execute(
        select(CandidateProfile).where(CandidateProfile.opportunity_id == opportunity_id)
    ).scalars().all():
        for emp in db.execute(
            select(Employee).where(Employee.candidate_profile_id == profile.id)
        ).scalars().all():
            emp.candidate_profile_id = None
        purge_profile_interviews(db, profile.id)
        db.delete(profile)
    db.flush()
    for req in db.execute(
        select(Requirement).where(Requirement.opportunity_id == opportunity_id)
    ).scalars().all():
        purge_requirement_owned(db, req.id)
        db.delete(req)
    for att in db.execute(
        select(OpportunityAttachment).where(OpportunityAttachment.opportunity_id == opportunity_id)
    ).scalars().all():
        db.delete(att)
    for tr in db.execute(
        select(TemplateRequest).where(TemplateRequest.opportunity_id == opportunity_id)
    ).scalars().all():
        tr.opportunity_id = None


def cascade_candidate_children(db: Session, candidate_id: int) -> None:
    """Cascade candidate-owned profiles, AI links, and slot bookings."""
    for link in db.execute(
        select(AiInterviewLink).where(AiInterviewLink.candidate_id == candidate_id)
    ).scalars().all():
        db.delete(link)
    db.flush()
    for profile in db.execute(
        select(CandidateProfile).where(CandidateProfile.candidate_id == candidate_id)
    ).scalars().all():
        for emp in db.execute(
            select(Employee).where(Employee.candidate_profile_id == profile.id)
        ).scalars().all():
            emp.candidate_profile_id = None
        purge_profile_interviews(db, profile.id)
        db.delete(profile)
    db.flush()
    for ev in db.execute(
        select(InterviewEvent).where(InterviewEvent.candidate_id == candidate_id)
    ).scalars().all():
        db.delete(ev)
    db.flush()
    for book in db.execute(
        select(SlotBooking).where(SlotBooking.candidate_id == candidate_id)
    ).scalars().all():
        db.delete(book)


def cascade_customer_owned_policies(db: Session, customer_id: int) -> None:
    """Remove leave policies + holidays owned by the customer (no finance impact)."""
    from models import BranchHolidayYear, CustomerBranch

    for detail in db.execute(
        select(ProjectEmployeeLeaveDetail).where(
            ProjectEmployeeLeaveDetail.customer_leave_policy_id.in_(
                select(CustomerLeavePolicy.id).where(
                    CustomerLeavePolicy.customer_id == customer_id
                )
            )
        )
    ).scalars().all():
        detail.customer_leave_policy_id = None
    for pol in db.execute(
        select(CustomerLeavePolicy).where(CustomerLeavePolicy.customer_id == customer_id)
    ).scalars().all():
        db.delete(pol)

    branch_ids = [
        r[0]
        for r in db.execute(
            select(CustomerBranch.id).where(CustomerBranch.customer_id == customer_id)
        ).all()
    ]
    clauses = [Holiday.customer_id == customer_id]
    if branch_ids:
        clauses.append(Holiday.branch_id.in_(branch_ids))
        year_ids = [
            r[0]
            for r in db.execute(
                select(BranchHolidayYear.id).where(BranchHolidayYear.branch_id.in_(branch_ids))
            ).all()
        ]
        if year_ids:
            clauses.append(Holiday.holiday_calendar_id.in_(year_ids))
    for hol in db.execute(select(Holiday).where(or_(*clauses))).scalars().all():
        db.delete(hol)


def soft_clear_leave_apps(db: Session, *, project_id: int | None = None, pe_id: int | None = None) -> None:
    """Null project/PE FKs on non-approved leave applications."""
    q = select(LeaveApplication)
    if project_id is not None:
        q = q.where(LeaveApplication.project_id == project_id)
    if pe_id is not None:
        q = q.where(LeaveApplication.project_employee_id == pe_id)
    for app in db.execute(q).scalars().all():
        if app.status == "Approved":
            continue
        if project_id is not None:
            app.project_id = None
        if pe_id is not None:
            app.project_employee_id = None


#: FK-ordered hard delete of opportunities. The ORM cascade above missed the
#: rows the NEXUS import created (resumes on the requirement, AI links keyed by
#: profile, requirement children), so the delete button 409'd on every
#: imported opportunity (8 Sep 2026, user report). One explicit statement per
#: child table, children first — the same list the import/wipe tools use.
_OPP_DELETE_SQL = [
    "DELETE FROM ai_interview_links WHERE opportunity_id IN :ids OR profile_id IN ({profs})",
    "DELETE FROM slot_bookings WHERE requirement_id IN ({reqs}) OR resume_id IN ({resumes})",
    "DELETE FROM interview_slots WHERE requirement_id IN ({reqs})",
    "UPDATE resumes SET possible_duplicate_of = NULL WHERE requirement_id IN ({reqs})",
    "DELETE FROM resumes WHERE requirement_id IN ({reqs})",
    "UPDATE template_requests SET opportunity_id = NULL WHERE opportunity_id IN :ids",
    "DELETE FROM template_requests WHERE requirement_id IN ({reqs})",
    "UPDATE employees SET candidate_profile_id = NULL WHERE candidate_profile_id IN ({profs})",
    "DELETE FROM interview_events WHERE profile_id IN ({profs})",
    "DELETE FROM offer_history WHERE profile_id IN ({profs})",
    "DELETE FROM skill_evaluations WHERE profile_id IN ({profs})",
    "DELETE FROM candidate_profile_activity_log WHERE profile_id IN ({profs})",
    "DELETE FROM candidate_profiles WHERE opportunity_id IN :ids",
    "DELETE FROM requirement_attachments WHERE requirement_id IN ({reqs})",
    "DELETE FROM requirement_skills WHERE requirement_id IN ({reqs})",
    "DELETE FROM requirement_job_postings WHERE requirement_id IN ({reqs})",
    "DELETE FROM requirement_activity_log WHERE requirement_id IN ({reqs})",
    "DELETE FROM requirements WHERE opportunity_id IN :ids",
    "DELETE FROM opportunity_skills WHERE opportunity_id IN :ids",
    "DELETE FROM opportunity_activity_log WHERE opportunity_id IN :ids",
    "DELETE FROM opportunity_attachments WHERE opportunity_id IN :ids",
    "DELETE FROM opportunity_ctc_slab WHERE opportunity_id IN :ids",
    "DELETE FROM opportunities WHERE id IN :ids",
]


def hard_delete_opportunities(db: Session, opportunity_ids: list[int]) -> None:
    """Delete opportunities and every recruiting record under them (requirement,
    applicant profiles, resumes, rounds, AI links, slots, attachments, CTC slab).
    Does NOT commit. Callers must block when a Project is built on one."""
    from sqlalchemy import bindparam, text

    ids = [int(i) for i in opportunity_ids]
    if not ids:
        return
    sub = {
        "reqs": "SELECT id FROM requirements WHERE opportunity_id IN :ids",
        "profs": "SELECT id FROM candidate_profiles WHERE opportunity_id IN :ids",
    }
    sub["resumes"] = f"SELECT id FROM resumes WHERE requirement_id IN ({sub['reqs']})"
    for sql in _OPP_DELETE_SQL:
        db.execute(text(sql.format(**sub)).bindparams(bindparam("ids", expanding=True)), {"ids": ids})
    db.expire_all()
