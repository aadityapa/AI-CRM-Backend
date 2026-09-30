"""CEO dashboard — four FY-anchored tabs on the Dashboard (28 Sep 2026).

    Finance  · what we billed, collected, are owed, earn (margin), and where cash lands
    Customer · customer → project → employee revenue drill-down
    Sales    · positions brought in, filled (internal vs external), open, pace
    People   · headcount, deployed vs bench, joiners / exits, roll-off radar

Everything is COMPOSED from the report modules that already own the rules —
`revenue_report` (invoice window, people cost, targets, ageing, cash flow),
`placements_report.classify` (the ONE internal-vs-external rule),
`hiring_dashboard` (positions, onboardings, pace, funnel) and
`project_closure.deployment_by_employee` (who is on the bench today) — so a
figure here can never disagree with the page it came from. Nothing is
recomputed with a second definition; where a tab needs a cut those modules do
not expose (month-by-month inside the period, the drill-down tree) it is built
from THEIR row loaders, not from a new query with a new rule.

`month` is the ANCHOR at every zoom, exactly like the Revenue report:
`?month=2026-09&period=fy` is FY 2026-27. The default zoom is the FY because
that is how the CEO asked to read it; quarter / month come for free.

Admin / CEO only (`role_required()` on the route) — the Customer tab prints
every customer's revenue and every employee's cost.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date, timedelta

import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.orm import Session

from models import (
    Candidate, CandidateProfile, CustomerBranch, Employee, Invoice, Opportunity, POStatus,
    PaymentStatus, PipelineStatus, Project, PurchaseOrder,
)
from models.finance import InvoiceKind
from services import hiring_dashboard as hiring
from services.dashboards import bench_rolloffs
from services.placements_report import (
    EXTERNAL, INTERNAL, UNKNOWN, Placement, _placements, _prior_index, classify, placements_report,
)
from services.project_closure import BENCH, DEPLOYED, deployment_by_employee
from services.revenue_report import (
    DEFAULT_TERMS_DAYS, SERIES_BY_PERIOD, Month, Period, _deployed_rows, _invoices_in_window,
    _monthly_rate, _overlap_fraction, _payments_in_window, _user_names, revenue_report,
)

logger = logging.getLogger("karnex.crm.executive_dashboard")

TABS = ("finance", "customer", "sales", "people")
DEFAULT_PERIOD = "fy"

#: Slices in the customer donut before "Others".
DONUT_SLICES = 6
#: One customer at or above this share of billing is a concentration risk.
CONCENTRATION_WARN_PCT = 50.0
#: Roll-off radar horizon on the People tab.
ROLLOFF_DAYS = 90
#: Names listed under an open position before "+N more".
MAX_CANDIDATES_PER_POSITION = 12
#: A customer's PO balance covering fewer months of its current burn is an alert.
PO_COVER_WARN_MONTHS = 2
#: A customer billing this much less than the previous period is an alert.
CUSTOMER_DROP_WARN_PCT = 30.0
#: A position open this long is stale.
STALE_POSITION_DAYS = 30
#: Bench cost above this share of the month's people cost is an alert.
BENCH_COST_WARN_PCT = 10.0
#: Attrition (exits ÷ average headcount in the period) above this is an alert.
ATTRITION_WARN_PCT = 20.0
#: Tenure buckets on the People tab (upper bound in years, label).
TENURE_BUCKETS = ((1, "< 1 yr"), (2, "1–2 yrs"), (4, "2–4 yrs"), (None, "4+ yrs"))


def _alert(out: list[dict], key: str, level: str, title: str, detail: str) -> None:
    """The Revenue report's alert shape (`key · level · title · detail`), so the
    same chip strip renders every tab."""
    out.append({"key": key, "level": level, "title": title, "detail": detail})


# --------------------------------------------------------------------------- helpers


def _pct(part: float, whole: float) -> float | None:
    return round(part * 100.0 / whole, 1) if whole else None


def _change_pct(now: float, before: float) -> float | None:
    return round((now - before) * 100.0 / before, 1) if before else None


def _months_in(period: Period) -> list[Month]:
    """Every calendar month inside the period, oldest first."""
    out, m = [], period.first_month
    for _ in range(period.months):
        out.append(m)
        m = m.shift(1)
    return out


def _period_view(period: Period, today: date) -> dict:
    return {
        "kind": period.kind, "key": period.key, "label": period.label,
        "short_label": period.short_label, "fy_label": period.fy_label,
        "anchor_month": period.anchor.key, "start": period.start.isoformat(),
        "end": period.end.isoformat(), "months": period.months,
        "is_current": period.contains(today), "comparison_label": period.comparison_label,
        "previous_key": period.previous.key,
    }


def _overlaps(row: dict, window) -> bool:
    """A deployment row (start / exit) that touches the window at all."""
    start, exit_ = row.get("start"), row.get("exit")
    if start and start > window.end:
        return False
    if exit_ and exit_ < window.start:
        return False
    return True


def _cost_in(row: dict, window) -> float:
    """People cost of one assignment inside the window — CTC / 12 × months × overlap,
    the SAME rule as `revenue_report._margin`."""
    if row.get("ctc") is None:
        return 0.0
    months = ((window.end.year - window.start.year) * 12 + window.end.month - window.start.month + 1)
    return row["ctc"] / 12.0 * months * _overlap_fraction(row.get("start"), row.get("exit"), window)


def _open_balances(db: Session, up_to: date, today: date) -> list[dict]:
    """Every open Tax invoice up to `up_to`, with its customer / project ids and
    whether it is past due today (the ageing rule: due_date, else +30 days)."""
    rows = db.execute(
        select(Invoice.balance_amount, Invoice.invoice_date, Invoice.due_date,
               Project.customer_id, Invoice.project_id)
        .join(Project, Project.id == Invoice.project_id)
        .where(Invoice.payment_status != PaymentStatus.PAID, Invoice.balance_amount > 0,
               Invoice.invoice_date <= up_to, Invoice.kind != InvoiceKind.PROFORMA.value)
    ).all()
    out = []
    for bal, inv_date, due, cid, pid in rows:
        effective_due = due or (inv_date + timedelta(days=DEFAULT_TERMS_DAYS) if inv_date else None)
        out.append({"balance": float(bal or 0), "customer_id": cid, "project_id": pid,
                    "overdue": bool(effective_due and effective_due < today)})
    return out


def _po_balance_by_customer(db: Session) -> dict[int, float]:
    rows = db.execute(
        select(PurchaseOrder.customer_id, sa.func.coalesce(sa.func.sum(PurchaseOrder.balance_value), 0))
        .where(PurchaseOrder.status == POStatus.ACTIVE)
        .group_by(PurchaseOrder.customer_id)
    ).all()
    return {cid: float(v or 0) for cid, v in rows}


UNSPECIFIED_LOCATION = "Unspecified"


def _project_locations(db: Session, project_ids: set[int]) -> dict[int, str]:
    """project_id → the customer LOCATION the project delivers at: the project's
    delivery branch (city, else branch name) → the opportunity's
    `tm_work_location` (the "Customer Location" HR sees) → Unspecified. An
    assignment has no location of its own, so this is the one rule."""
    if not project_ids:
        return {}
    rows = db.execute(
        select(Project.id, CustomerBranch.city, CustomerBranch.branch_name, Opportunity.details)
        .outerjoin(CustomerBranch, CustomerBranch.id == Project.branch_id)
        .outerjoin(Opportunity, Opportunity.id == Project.opportunity_id)
        .where(Project.id.in_(list(project_ids)))
    ).all()
    out = {}
    for pid, city, branch, details in rows:
        loc = (city or "").strip() or (branch or "").strip()
        if not loc and isinstance(details, dict):
            raw = details.get("tm_work_location")
            loc = ", ".join(str(p).strip() for p in raw if str(p).strip()) if isinstance(raw, (list, tuple)) else str(raw or "").strip()
        out[pid] = loc or UNSPECIFIED_LOCATION
    return out


def _deployed_trend(assignments: list[dict], locations: dict[int, str], period: Period,
                    today: date) -> tuple[dict, list[str]]:
    """Heads deployed at the END of each bucket, by customer and by location, at
    three zooms (`month` = the period's months; `quarter` / `fy` = the last
    8 / 5 ending at the anchor). A person on two customers counts once per
    customer and once in `total`. Buckets that have not ended yet read as of
    TODAY; a bucket that has not started is `future`. The `DONUT_SLICES`
    customers with the highest PEAK across every bucket are their own series
    (an account that was big last year must stay visible), the rest "Others".
    Returns (trend, customer series names)."""
    def _heads(b) -> tuple[dict[str, set], dict[str, set], set]:
        as_of = min(b.end, today)
        by_customer: dict[str, set] = defaultdict(set)
        by_location: dict[str, set] = defaultdict(set)
        heads: set[int] = set()
        if b.start <= today:
            for r in assignments:
                if (r["start"] and r["start"] > as_of) or (r["exit"] and r["exit"] < as_of):
                    continue
                by_customer[r["customer"]].add(r["employee_id"])
                by_location[locations.get(r["project_id"], UNSPECIFIED_LOCATION)].add(r["employee_id"])
                heads.add(r["employee_id"])
        return by_customer, by_location, heads

    buckets = {"month": [(m, m.start.strftime("%b %y")) for m in _months_in(period)]}
    for kind in ("quarter", "fy"):
        anchor = Period(kind, period.anchor)
        buckets[kind] = [(b, b.short_label) for b in (anchor.shift(-i) for i in range(SERIES_BY_PERIOD[kind] - 1, -1, -1))]
    counted = {kind: [(b, label, _heads(b)) for b, label in rows] for kind, rows in buckets.items()}

    peak: dict[str, int] = defaultdict(int)
    for rows in counted.values():
        for _b, _label, (by_customer, _l, _h) in rows:
            for name, ids in by_customer.items():
                peak[name] = max(peak[name], len(ids))
    top = [name for name, _ in sorted(peak.items(), key=lambda kv: (-kv[1], kv[0]))[:DONUT_SLICES]]
    series = top + (["Others"] if len(peak) > len(top) else [])

    def _view(b, label, parts) -> dict:
        by_customer, by_location, heads = parts
        customers: dict[str, int] = defaultdict(int)
        for name, ids in by_customer.items():
            customers[name if name in top else "Others"] += len(ids)
        return {"key": b.key, "label": label, "future": b.start > today, "total": len(heads),
                "customers": dict(customers), "locations": {k: len(v) for k, v in by_location.items()}}

    return {kind: [_view(b, label, parts) for b, label, parts in rows] for kind, rows in counted.items()}, series


def _kind_index(db: Session, employee_ids: set[int]) -> dict[int, tuple[str, str]]:
    """pe_id → (internal | external | unknown, reason) for every placement of
    these people — `placements_report.classify`, the one rule, applied over the
    person's whole history so a redeployment is always internal."""
    if not employee_ids:
        return {}
    history = _placements(db, employee_ids=employee_ids)
    prior = _prior_index(history)
    out: dict[int, tuple[str, str]] = {}
    for p in history:
        if p.placed_on is None:
            out[p.pe_id] = (UNKNOWN, "No onboarding date on the assignment")
            continue
        kind, reason, _gap = classify(p.placed_on, p.karnex_joined, prior.get(p.pe_id))
        out[p.pe_id] = (kind, reason)
    return out


# --------------------------------------------------------------------------- finance


def _finance(db: Session, period: Period, today: date) -> dict:
    """The Revenue report's sections the CEO reads first, plus a month-by-month
    cut INSIDE the period (the report's own trend is 12 months / 8 quarters /
    5 FYs — at the FY zoom it cannot show how this year unfolded)."""
    rep = revenue_report(db, period.anchor.key, today, period.kind)
    invoices = _invoices_in_window(db, period.start, period.end)
    payments = _payments_in_window(db, period.start, period.end)
    deployed = _deployed_rows(db)

    monthly = []
    for m in _months_in(period):
        billed = sum(i["sub_total"] for i in invoices if i["invoice_date"] and m.start <= i["invoice_date"] <= m.end)
        collected = sum(p["amount"] for p in payments if p["date"] and m.start <= p["date"] <= m.end)
        cost = sum(_cost_in(r, m) for r in deployed if _overlaps(r, m))
        future = m.start > today
        monthly.append({
            "key": m.key, "label": m.start.strftime("%b %y"), "billed": round(billed, 2),
            "collected": round(collected, 2), "cost": round(cost, 2),
            "margin": round(billed - cost, 2), "future": future,
        })

    by_type = rep["dimensions"]["by_type"]
    return {
        "headline": rep["headline"],
        "targets": rep["targets"],
        "margin": {k: v for k, v in rep["margin"].items() if k != "by_customer"},
        "collections": rep["collections"],
        "ageing": rep["ageing"],
        "cashflow": rep["cashflow"],
        "pipeline": rep["pipeline"],
        "efficiency": rep["efficiency"],
        "alerts": rep["alerts"],
        "series": rep["series"],
        "monthly": monthly,
        "by_type": by_type,
        "top_customers": rep["by_customer"]["rows"][:DONUT_SLICES],
        "concentration": {
            "top1_share_pct": rep["by_customer"]["top1_share_pct"],
            "top3_share_pct": rep["by_customer"]["top3_share_pct"],
            "risk": rep["by_customer"]["concentration_risk"],
            "top_customer": rep["by_customer"]["top_customer"],
        },
    }


# --------------------------------------------------------------------------- customer


def _customer(db: Session, period: Period, today: date) -> dict:
    """Customer → project → employee. Revenue is Tax invoices by `invoice_date`
    (sub_total, excl. GST); an invoice reaches an EMPLOYEE only through its
    timesheet, so a manually raised invoice stays at the project as
    `unlinked_billed` and is never dropped. Cost is CTC / 12 × months × overlap."""
    previous = period.previous
    invoices = _invoices_in_window(db, period.start, period.end)
    prev_invoices = _invoices_in_window(db, previous.start, previous.end)
    payments = _payments_in_window(db, period.start, period.end)
    balances = _open_balances(db, period.end, today)
    assignments = _deployed_rows(db)
    deployed = [r for r in assignments if _overlaps(r, period)]
    po_balance = _po_balance_by_customer(db)
    reqs = hiring._requirement_rows(db)
    positions = {r["id"]: r for r in hiring._customers(reqs)["rows"]}
    kinds = _kind_index(db, {r["employee_id"] for r in deployed})
    live_today = deployment_by_employee(db, {r["employee_id"] for r in deployed}, today)

    # ---- roll the facts up by customer / project / pe
    cust_billed: dict[int, float] = defaultdict(float)
    cust_prev: dict[int, float] = defaultdict(float)
    cust_collected: dict[int, float] = defaultdict(float)
    cust_out: dict[int, float] = defaultdict(float)
    cust_overdue: dict[int, float] = defaultdict(float)
    cust_invoices: dict[int, int] = defaultdict(int)
    proj_billed: dict[int, float] = defaultdict(float)
    proj_prev: dict[int, float] = defaultdict(float)
    proj_collected: dict[int, float] = defaultdict(float)
    proj_out: dict[int, float] = defaultdict(float)
    proj_invoices: dict[int, int] = defaultdict(int)
    proj_unlinked: dict[int, float] = defaultdict(float)
    emp_billed: dict[tuple[int, int], float] = defaultdict(float)   # (project, employee)
    emp_invoices: dict[tuple[int, int], int] = defaultdict(int)
    names: dict[int, str] = {}
    proj_names: dict[int, tuple[str, int]] = {}
    for i in invoices:
        cid, pid = i["customer_id"], i["project_id"]
        names[cid] = i["customer"]
        proj_names[pid] = (i["project"], cid)
        cust_billed[cid] += i["sub_total"]
        cust_invoices[cid] += 1
        proj_billed[pid] += i["sub_total"]
        proj_invoices[pid] += 1
        if i["employee_id"]:
            emp_billed[(pid, i["employee_id"])] += i["sub_total"]
            emp_invoices[(pid, i["employee_id"])] += 1
        else:
            proj_unlinked[pid] += i["sub_total"]
    for i in prev_invoices:
        cust_prev[i["customer_id"]] += i["sub_total"]
        proj_prev[i["project_id"]] += i["sub_total"]
    for p in payments:
        cust_collected[p["customer_id"]] += p["amount"]
        proj_collected[p["project_id"]] += p["amount"]
    for b in balances:
        cust_out[b["customer_id"]] += b["balance"]
        proj_out[b["project_id"]] += b["balance"]
        if b["overdue"]:
            cust_overdue[b["customer_id"]] += b["balance"]
    for r in deployed:
        names.setdefault(r["customer_id"], r["customer"])
        proj_names.setdefault(r["project_id"], (r["project"], r["customer_id"]))

    # Projects that neither billed nor had a head in the period are not part
    # of this period's story; their status / end date still come from the table.
    project_meta = {
        pid: {"status": getattr(st, "value", st), "end_date": end.isoformat() if end else None,
              "opportunity": title, "opp_id": opp_id}
        for pid, st, end, title, opp_id in db.execute(
            select(Project.id, Project.status, Project.end_date, Opportunity.title, Opportunity.opp_id)
            .outerjoin(Opportunity, Opportunity.id == Project.opportunity_id)
            .where(Project.id.in_(list(proj_names) or [0]))
        ).all()
    }

    total_billed = sum(cust_billed.values())
    customers = []
    for cid in {*names}:
        billed = cust_billed.get(cid, 0.0)
        c_rows = [r for r in deployed if r["customer_id"] == cid]
        cost = sum(_cost_in(r, period) for r in c_rows)
        pids = sorted({p for p, (_, c) in proj_names.items() if c == cid},
                      key=lambda p: -proj_billed.get(p, 0.0))
        projects = []
        for pid in pids:
            p_rows = [r for r in c_rows if r["project_id"] == pid]
            p_billed = proj_billed.get(pid, 0.0)
            p_cost = sum(_cost_in(r, period) for r in p_rows)
            employees = []
            for r in sorted(p_rows, key=lambda r: -emp_billed.get((pid, r["employee_id"]), 0.0)):
                kind, reason = kinds.get(r["pe_id"], (UNKNOWN, "No onboarding date on the assignment"))
                e_billed = emp_billed.get((pid, r["employee_id"]), 0.0)
                e_cost = _cost_in(r, period)
                employees.append({
                    "pe_id": r["pe_id"], "employee_id": r["employee_id"], "employee": r["name"],
                    "employee_code": r["employee_code"], "rate": r["rate"],
                    "unit": getattr(r["unit"], "value", r["unit"]),
                    "monthly_rate": round(_monthly_rate(r["rate"], r["unit"]), 2),
                    "billed": round(e_billed, 2), "invoices": emp_invoices.get((pid, r["employee_id"]), 0),
                    "cost": round(e_cost, 2), "margin": round(e_billed - e_cost, 2),
                    "has_ctc": r["ctc"] is not None,
                    "onboarding": r["start"].isoformat() if r["start"] else None,
                    "exit": (r["exit"].isoformat() if r["exit"] and r["exit"].year > 1900 else None),
                    "exited": r["is_exit"] or bool(r["exit"] and r["exit"] < today),
                    "deployed_today": live_today.get(r["employee_id"], {}).get("status") == DEPLOYED,
                    "kind": kind, "kind_reason": reason,
                })
            meta = project_meta.get(pid, {})
            projects.append({
                "project_id": pid, "project": proj_names[pid][0],
                "status": meta.get("status"), "end_date": meta.get("end_date"),
                "opportunity": meta.get("opportunity"), "opp_id": meta.get("opp_id"),
                "billed": round(p_billed, 2), "previous": round(proj_prev.get(pid, 0.0), 2),
                "change_pct": _change_pct(p_billed, proj_prev.get(pid, 0.0)),
                "share_pct": _pct(p_billed, billed), "invoices": proj_invoices.get(pid, 0),
                "collected": round(proj_collected.get(pid, 0.0), 2),
                "outstanding": round(proj_out.get(pid, 0.0), 2),
                "unlinked_billed": round(proj_unlinked.get(pid, 0.0), 2),
                "cost": round(p_cost, 2), "margin": round(p_billed - p_cost, 2),
                "margin_pct": _pct(p_billed - p_cost, p_billed),
                "heads": len(p_rows),
                "heads_today": sum(1 for e in employees if e["deployed_today"] and not e["exited"]),
                "internal": sum(1 for e in employees if e["kind"] == INTERNAL),
                "external": sum(1 for e in employees if e["kind"] == EXTERNAL),
                "employees": employees,
            })
        pos = positions.get(cid, {})
        # Today's burn = the monthly billing rate of everyone deployed right now;
        # PO cover = how many months the active PO balance funds that burn.
        burn = sum(e["monthly_rate"] for p in projects for e in p["employees"]
                   if e["deployed_today"] and not e["exited"])
        customers.append({
            "customer_id": cid, "customer": names[cid] or f"#{cid}",
            "billed": round(billed, 2), "previous": round(cust_prev.get(cid, 0.0), 2),
            "change_pct": _change_pct(billed, cust_prev.get(cid, 0.0)),
            "share_pct": _pct(billed, total_billed), "invoices": cust_invoices.get(cid, 0),
            "collected": round(cust_collected.get(cid, 0.0), 2),
            "outstanding": round(cust_out.get(cid, 0.0), 2),
            "overdue": round(cust_overdue.get(cid, 0.0), 2),
            "cost": round(cost, 2), "margin": round(billed - cost, 2),
            "margin_pct": _pct(billed - cost, billed),
            "heads": len(c_rows),
            "heads_today": sum(p["heads_today"] for p in projects),
            "internal": sum(p["internal"] for p in projects),
            "external": sum(p["external"] for p in projects),
            "projects_count": len(projects),
            "po_balance": round(po_balance.get(cid, 0.0), 2),
            "monthly_burn": round(burn, 2),
            "po_cover_months": round(po_balance.get(cid, 0.0) / burn, 1) if burn else None,
            "open_positions": pos.get("open_positions", 0),
            "total_positions": pos.get("total_positions", 0),
            "joined_positions": pos.get("joined", 0),
            "projects": projects,
        })
    customers.sort(key=lambda c: (-c["billed"], c["customer"].lower()))

    shares = [c["billed"] for c in customers]
    top1 = _pct(shares[0], total_billed) if shares else None
    top3 = _pct(sum(shares[:3]), total_billed) if shares else None
    donut = [{"label": c["customer"], "value": c["billed"], "customer_id": c["customer_id"]}
             for c in customers[:DONUT_SLICES] if c["billed"] > 0]
    rest = sum(c["billed"] for c in customers[DONUT_SLICES:])
    if rest > 0:
        donut.append({"label": f"Others ({len(customers) - DONUT_SLICES})", "value": round(rest, 2),
                      "customer_id": None})

    # ---- how the period unfolded, customer by customer: the top customers are
    # their own series, everyone else is "Others", so the chart stays readable.
    top_ids = [c["customer_id"] for c in customers[:DONUT_SLICES] if c["billed"] > 0]
    top_names = {c["customer_id"]: c["customer"] for c in customers}
    others_label = f"Others ({len(customers) - len(top_ids)})" if len(customers) > len(top_ids) else None
    monthly = []
    for m in _months_in(period):
        rows = [i for i in invoices if i["invoice_date"] and m.start <= i["invoice_date"] <= m.end]
        per: dict[str, float] = defaultdict(float)
        for i in rows:
            label = top_names[i["customer_id"]] if i["customer_id"] in top_ids else others_label
            if label:
                per[label] += i["sub_total"]
        monthly.append({
            "key": m.key, "label": m.start.strftime("%b %y"), "future": m.start > today,
            "billed": round(sum(i["sub_total"] for i in rows), 2),
            "collected": round(sum(p["amount"] for p in payments if p["date"] and m.start <= p["date"] <= m.end), 2),
            "cost": round(sum(_cost_in(r, m) for r in deployed if _overlaps(r, m)), 2),
            "customers": {k: round(v, 2) for k, v in per.items()},
        })
    series_names = [top_names[c] for c in top_ids] + ([others_label] if others_label else [])

    # ---- deployed heads over time, by customer and by customer location — the
    # quarter / FY zooms look back 8 / 5 buckets, so EVERY assignment is read.
    locations = _project_locations(db, {r["project_id"] for r in assignments})
    deployed_trend, deployed_customers = _deployed_trend(assignments, locations, period, today)
    location_names = sorted({loc for b in deployed_trend.values() for r in b for loc in r["locations"]})

    alerts: list[dict] = []
    if top1 is not None and top1 >= CONCENTRATION_WARN_PCT:
        _alert(alerts, "concentration", "warn", f"{customers[0]['customer']} is {top1:.0f}% of billing",
               "One customer at or above half the period's billing — a renewal risk the CEO should know by name.")
    for c in customers:
        if c["overdue"] > 0:
            _alert(alerts, f"overdue:{c['customer_id']}", "bad", f"{c['customer']} owes ₹{c['overdue']:,.0f} overdue",
                   f"₹{c['outstanding']:,.0f} outstanding in total — chase before the next invoice.")
        if c["po_cover_months"] is not None and c["po_cover_months"] < PO_COVER_WARN_MONTHS:
            _alert(alerts, f"po_cover:{c['customer_id']}", "warn",
                   f"{c['customer']} PO covers {c['po_cover_months']} month(s)",
                   f"₹{c['po_balance']:,.0f} left against a burn of ₹{c['monthly_burn']:,.0f} / month — raise the renewal.")
        if c["change_pct"] is not None and c["change_pct"] <= -CUSTOMER_DROP_WARN_PCT and c["previous"] > 0:
            _alert(alerts, f"drop:{c['customer_id']}", "warn",
                   f"{c['customer']} down {abs(c['change_pct']):.0f}% {period.comparison_label}",
                   f"₹{c['billed']:,.0f} now against ₹{c['previous']:,.0f} — find out why before the account fades.")
        if c["billed"] > 0 and c["margin"] < 0:
            _alert(alerts, f"loss:{c['customer_id']}", "bad", f"{c['customer']} is loss-making",
                   f"Billed ₹{c['billed']:,.0f} against ₹{c['cost']:,.0f} of people cost.")
    return {
        "summary": {
            "customers": len(customers),
            "customers_billing": sum(1 for c in customers if c["billed"] > 0),
            "billed": round(total_billed, 2),
            "previous": round(sum(cust_prev.values()), 2),
            "change_pct": _change_pct(total_billed, sum(cust_prev.values())),
            "collected": round(sum(cust_collected.values()), 2),
            "outstanding": round(sum(cust_out.values()), 2),
            "overdue": round(sum(cust_overdue.values()), 2),
            "heads": len(deployed),
            "heads_today": sum(c["heads_today"] for c in customers),
            "top1_share_pct": top1, "top3_share_pct": top3,
            "concentration_risk": bool(top1 is not None and top1 >= CONCENTRATION_WARN_PCT),
            "top_customer": customers[0]["customer"] if customers else None,
        },
        "donut": donut,
        "monthly": monthly,
        "monthly_series": series_names,
        "deployed_trend": deployed_trend,
        "deployed_series": {"customers": deployed_customers, "locations": location_names},
        "alerts": alerts,
        "customers": customers,
    }


# --------------------------------------------------------------------------- sales


def _joined_kinds(db: Session, joins: list[hiring.JoinRow]) -> dict[int, tuple[str, str, str]]:
    """profile_id → (kind, reason, employee name) for every Joined profile.

    A joined candidate has an Employee record (`Employee.candidate_profile_id`,
    written at Joined; else the Emp ID HR typed as `employee_ref`). The
    placement date is the joining day, the Karnex joining date is the
    employee's `date_of_joining`, and the prior placement is that person's
    last assignment BEFORE it — the SAME `classify` the placements report
    uses, so "internal" means one thing everywhere. No employee record yet →
    unknown, never guessed.
    """
    if not joins:
        return {}
    pids = [j.profile_id for j in joins]
    ref_rows = db.execute(
        select(CandidateProfile.id, CandidateProfile.employee_ref)
        .where(CandidateProfile.id.in_(pids), CandidateProfile.employee_ref.isnot(None))
    ).all()
    refs = {pid: (ref or "").strip() for pid, ref in ref_rows if (ref or "").strip()}
    emp_rows = db.execute(
        select(Employee.id, Employee.candidate_profile_id, Employee.employee_code,
               Employee.date_of_joining, Employee.first_name, Employee.last_name)
        .where(sa.or_(Employee.candidate_profile_id.in_(pids),
                      Employee.employee_code.in_(list(refs.values()) or ["-"])))
    ).all()
    by_profile: dict[int, tuple] = {}
    by_code: dict[str, tuple] = {}
    for eid, cpid, code, doj, fn, ln in emp_rows:
        row = (eid, doj, " ".join(p for p in (fn, ln) if p))
        if cpid:
            by_profile[cpid] = row
        if code:
            by_code[code.strip()] = row
    emp_for: dict[int, tuple] = {}
    for j in joins:
        row = by_profile.get(j.profile_id) or (by_code.get(refs[j.profile_id]) if j.profile_id in refs else None)
        if row:
            emp_for[j.profile_id] = row
    history = _placements(db, employee_ids={r[0] for r in emp_for.values()})
    by_emp: dict[int, list[Placement]] = defaultdict(list)
    for p in history:
        if p.placed_on is not None:
            by_emp[p.employee_id].append(p)
    out: dict[int, tuple[str, str, str]] = {}
    for j in joins:
        row = emp_for.get(j.profile_id)
        if not row:
            out[j.profile_id] = (UNKNOWN, "No employee record linked to this candidate yet", "")
            continue
        eid, doj, name = row
        earlier = [p for p in by_emp.get(eid, []) if p.placed_on < j.joined_on]
        prior = max(earlier, key=lambda p: (p.placed_on, p.pe_id)) if earlier else None
        kind, reason, _gap = classify(j.joined_on, doj, prior)
        out[j.profile_id] = (kind, reason, name)
    return out


def _onboarding_trend(reqs: list[hiring.ReqRow], joins: list[hiring.JoinRow], kinds: dict,
                      period: Period, today: date) -> dict:
    """Positions brought in and onboardings (split internal / external /
    unknown) at THREE zooms at once — the CEO asked to flip Monthly ·
    Quarterly · Yearly without a reload. `month` = the months of the selected
    period; `quarter` / `fy` = the last 8 quarters / 5 FYs ending at the anchor
    (the report's trend depth), oldest first; a bucket that has not started
    yet is `future`. `positions_in` = `no_of_positions` of requirements CREATED
    in the bucket — the tower's "pipeline positions" rule."""
    def _bucket(b, label: str) -> dict:
        rows = [j for j in joins if b.start <= j.joined_on <= b.end]
        k = [kinds.get(j.profile_id, (UNKNOWN,))[0] for j in rows]
        return {"key": b.key, "label": label, "onboardings": len(rows),
                "positions_in": sum(r.positions for r in reqs if b.start <= r.created_on <= b.end),
                "internal": k.count(INTERNAL), "external": k.count(EXTERNAL), "unknown": k.count(UNKNOWN),
                "future": b.start > today}
    out = {"month": [_bucket(m, m.start.strftime("%b %y")) for m in _months_in(period)]}
    for kind in ("quarter", "fy"):
        anchor = Period(kind, period.anchor)
        n = SERIES_BY_PERIOD[kind]
        out[kind] = [_bucket(b, b.short_label) for b in (anchor.shift(-i) for i in range(n - 1, -1, -1))]
    return out


def _sales(db: Session, period: Period, today: date) -> dict:
    """The hiring control tower's numbers (same module, same definitions) plus
    the position-by-position table the CEO asked for: every open or period-
    touched position with how many joined, internal vs external, and who."""
    tower = hiring.hiring_dashboard(db, period.anchor.key, period.kind, today)
    reqs = hiring._requirement_rows(db)
    joins = hiring._joined_rows(db)
    kinds = _joined_kinds(db, joins)

    terminal = {s.value for s in hiring.TERMINAL_STATUSES}
    rejected = {s.value for s in hiring.REJECTED_STATUSES}
    workable = {s.value for s in hiring.WORKABLE_STATUSES}

    def _live(r: hiring.ReqRow) -> bool:
        return r.status not in terminal and r.status not in rejected and r.opp_approved

    shown = [r for r in reqs if _live(r) or period.contains(r.created_on)
             or (r.closed_on and period.contains(r.closed_on))]
    opp_ids = {r.opportunity_id for r in shown}
    opps = {
        oid: {"opp_id": code, "title": title, "owner_id": owner,
              "opp_type": getattr(t, "value", t), "rfi_value": float(rfi) if rfi is not None else None}
        for oid, code, title, owner, t, rfi in db.execute(
            select(Opportunity.id, Opportunity.opp_id, Opportunity.title, Opportunity.created_by,
                   Opportunity.opp_type, Opportunity.rfi_value)
            .where(Opportunity.id.in_(list(opp_ids) or [0]))
        ).all()
    }
    owners = _user_names(db, {o["owner_id"] for o in opps.values() if o["owner_id"]})
    cand_names = {
        pid: name for pid, name in db.execute(
            select(CandidateProfile.id,
                   sa.func.trim(sa.func.coalesce(Candidate.first_name, "") + " "
                                + sa.func.coalesce(Candidate.last_name, "")))
            .join(Candidate, Candidate.id == CandidateProfile.candidate_id)
            .where(CandidateProfile.opportunity_id.in_(list(opp_ids) or [0]),
                   CandidateProfile.pipeline_status == PipelineStatus.JOINED)
        ).all()
    }
    joins_by_opp: dict[int, list[hiring.JoinRow]] = defaultdict(list)
    for j in joins:
        joins_by_opp[j.opportunity_id].append(j)

    positions = []
    for r in sorted(shown, key=lambda r: (-r.open, r.created_on)):
        o = opps.get(r.opportunity_id, {})
        cands = []
        for j in sorted(joins_by_opp.get(r.opportunity_id, []), key=lambda j: j.joined_on):
            kind, reason, emp_name = kinds.get(j.profile_id, (UNKNOWN, "", ""))
            cands.append({"profile_id": j.profile_id,
                          "name": cand_names.get(j.profile_id) or emp_name or f"Candidate #{j.profile_id}",
                          "joined_on": j.joined_on.isoformat(), "kind": kind, "reason": reason,
                          "in_period": period.contains(j.joined_on)})
        positions.append({
            "requirement_id": r.id, "opportunity_id": r.opportunity_id,
            "opp_id": o.get("opp_id"), "title": o.get("title") or f"Opportunity #{r.opportunity_id}",
            "customer_id": r.customer_id, "customer": r.customer_name,
            "owner": owners.get(o.get("owner_id")) if o.get("owner_id") else None,
            "opp_type": o.get("opp_type"), "rfi_value": o.get("rfi_value"),
            "status": r.status, "opp_stage": r.opp_stage, "live": _live(r),
            "workable": r.status in workable and _live(r),
            "positions": r.positions, "joined": r.joined, "open": r.open,
            "fill_pct": _pct(min(r.joined, r.positions), r.positions),
            "created_on": r.created_on.isoformat(),
            "closed_on": r.closed_on.isoformat() if r.closed_on else None,
            "age_days": ((r.closed_on or today) - r.created_on).days,
            "created_in_period": period.contains(r.created_on),
            "joined_in_period": sum(1 for c in cands if c["in_period"]),
            "internal": sum(1 for c in cands if c["kind"] == INTERNAL),
            "external": sum(1 for c in cands if c["kind"] == EXTERNAL),
            "unknown": sum(1 for c in cands if c["kind"] == UNKNOWN),
            "candidates": cands[:MAX_CANDIDATES_PER_POSITION],
            "candidates_more": max(0, len(cands) - MAX_CANDIDATES_PER_POSITION),
        })

    in_period = [j for j in joins if period.contains(j.joined_on)]
    mix = {INTERNAL: 0, EXTERNAL: 0, UNKNOWN: 0}
    for j in in_period:
        mix[kinds.get(j.profile_id, (UNKNOWN,))[0]] += 1
    monthly = []
    for m in _months_in(period):
        mj = [j for j in joins if m.start <= j.joined_on <= m.end]
        monthly.append({
            "key": m.key, "label": m.start.strftime("%b %y"),
            "positions_in": sum(r.positions for r in reqs if m.start <= r.created_on <= m.end),
            "onboardings": len(mj),
            "internal": sum(1 for j in mj if kinds.get(j.profile_id, (UNKNOWN,))[0] == INTERNAL),
            "external": sum(1 for j in mj if kinds.get(j.profile_id, (UNKNOWN,))[0] == EXTERNAL),
            "unknown": sum(1 for j in mj if kinds.get(j.profile_id, (UNKNOWN,))[0] == UNKNOWN),
            "future": m.start > today,
        })

    by_owner: dict[str, dict] = {}
    for p in positions:
        key = p["owner"] or "Unassigned"
        slot = by_owner.setdefault(key, {"owner": key, "positions": 0, "open": 0, "joined": 0,
                                         "opportunities": 0})
        if p["created_in_period"] or p["live"]:
            slot["opportunities"] += 1
            slot["positions"] += p["positions"]
            slot["open"] += p["open"] if p["live"] else 0
            slot["joined"] += p["joined"]

    # Live positions rolled up by customer — where the open headcount sits.
    by_customer: dict[int, dict] = {}
    for p in positions:
        if not p["live"]:
            continue
        slot = by_customer.setdefault(p["customer_id"], {
            "customer_id": p["customer_id"], "customer": p["customer"], "positions": 0, "open": 0,
            "joined": 0, "internal": 0, "external": 0, "opportunities": 0, "stale": 0})
        slot["opportunities"] += 1
        slot["positions"] += p["positions"]
        slot["open"] += p["open"]
        slot["joined"] += p["joined"]
        slot["internal"] += p["internal"]
        slot["external"] += p["external"]
        slot["stale"] += 1 if p["open"] > 0 and p["age_days"] >= STALE_POSITION_DAYS else 0

    stale = [p for p in positions if p["live"] and p["open"] > 0 and p["age_days"] >= STALE_POSITION_DAYS]
    alerts: list[dict] = []
    for role, label in (("sales", "Sales pace"), ("fulfilment", "Fulfilment pace")):
        pace = tower["pace"][role]
        if pace.get("state") == "bad" and pace.get("target"):
            _alert(alerts, f"pace:{role}", "bad", f"{label} needs {pace.get('acceleration') or 0:.1f}× the current rate",
                   f"{pace['actual']} of {pace['target']} so far — {pace.get('required_per_week') or 0:.1f} a week needed "
                   f"against {pace.get('current_per_week') or 0:.1f} now.")
    if stale:
        worst = max(stale, key=lambda p: p["age_days"])
        _alert(alerts, "stale_positions", "warn" if len(stale) < 3 else "bad",
               f"{len(stale)} position(s) open for {STALE_POSITION_DAYS}+ days",
               f"Oldest: {worst['title']} at {worst['customer']}, {worst['age_days']} days.")
    if tower["customers"].get("concentration_risk") and tower["customers"].get("largest"):
        big = tower["customers"]["largest"]
        _alert(alerts, "position_concentration", "warn", f"{big['name']} holds {big['share_pct']:.0f}% of open positions",
               "Half the open headcount depends on one account.")
    bad_stages = [r for r in tower["stage_delays"]["rows"] if r["state"] == "bad"]
    if bad_stages:
        _alert(alerts, "stage_delays", "bad", f"{sum(r['over_bad'] for r in bad_stages)} candidate(s) stuck past "
               f"{tower['stage_delays']['bad_days']} days", "Stages: " + ", ".join(r["label"] for r in bad_stages) + ".")
    if mix[UNKNOWN]:
        _alert(alerts, "unknown_kind", "warn", f"{mix[UNKNOWN]} onboarding(s) not linked to an employee record",
               "Internal vs external cannot be told until HR creates the employee — the mix is understated.")

    return {
        "kpis": tower["kpis"],
        "pace": tower["pace"],
        "targets": tower["targets"],
        "series": tower["series"],
        "funnel": tower["funnel"],
        "customers": tower["customers"],
        "stage_delays": tower["stage_delays"],
        "by_customer": sorted(by_customer.values(), key=lambda c: (-c["open"], -c["positions"], c["customer"])),
        "alerts": alerts,
        "onboarding_trend": _onboarding_trend(reqs, joins, kinds, period, today),
        "onboarding_mix": {
            "internal": mix[INTERNAL], "external": mix[EXTERNAL], "unknown": mix[UNKNOWN],
            "total": len(in_period),
            "internal_pct": _pct(mix[INTERNAL], mix[INTERNAL] + mix[EXTERNAL]),
        },
        "monthly": monthly,
        "positions": positions,
        "positions_summary": {
            "shown": len(positions),
            "live": sum(1 for p in positions if p["live"]),
            "open": sum(p["open"] for p in positions if p["live"]),
            "total": sum(p["positions"] for p in positions if p["live"]),
            "joined": sum(p["joined"] for p in positions if p["live"]),
            "stale_30": sum(1 for p in positions if p["live"] and p["open"] > 0 and p["age_days"] >= 30),
        },
        "by_owner": sorted(by_owner.values(), key=lambda o: (-o["positions"], o["owner"])),
        "rules": {
            "internal": "An earlier placement anywhere, or a Karnex joining date more than 30 days "
                        "before the onboarding (placements_report.classify).",
            "external": "Joined Karnex within 30 days of (or after) the onboarding, no earlier placement.",
            "unknown": "No employee record linked to the candidate yet.",
        },
    }


# --------------------------------------------------------------------------- people


def _people(db: Session, period: Period, today: date) -> dict:
    """Headcount, deployed vs bench, joiners / exits, and who rolls off next.

    Headcount = active employees not yet past their last working day. Exits
    are dated by `last_working_day`, else `date_of_resignation`. Bench cost is
    CTC / 12 per month of every bench head — the number the CEO feels."""
    rows = db.execute(
        select(Employee.id, Employee.first_name, Employee.last_name, Employee.employee_code,
               Employee.role_title, Employee.date_of_joining, Employee.current_ctc,
               Employee.is_active, Employee.is_resigned, Employee.date_of_resignation,
               Employee.last_working_day)
    ).all()
    emps = []
    for eid, fn, ln, code, role, doj, ctc, active, resigned, resigned_on, lwd in rows:
        exit_on = lwd or (resigned_on if resigned else None)
        emps.append({
            "id": eid, "name": " ".join(p for p in (fn, ln) if p) or f"Employee #{eid}",
            "code": code, "role": role, "doj": doj, "ctc": float(ctc) if ctc is not None else None,
            "active": bool(active), "resigned": bool(resigned), "exit_on": exit_on,
        })

    def _on_rolls(e: dict, on: date) -> bool:
        if e["doj"] and e["doj"] > on:
            return False
        if e["exit_on"] and e["exit_on"] < on:
            return False
        if not e["exit_on"] and not e["active"]:
            return False
        return True

    current = [e for e in emps if _on_rolls(e, today)]
    live = deployment_by_employee(db, {e["id"] for e in current}, today)
    deployed = [e for e in current if live.get(e["id"], {}).get("status") == DEPLOYED]
    bench = [e for e in current if live.get(e["id"], {}).get("status", BENCH) == BENCH]
    bench_cost = sum(e["ctc"] / 12.0 for e in bench if e["ctc"] is not None)
    joiners = [e for e in emps if e["doj"] and period.contains(e["doj"])]
    # Realised exits only — a resignation with a last day still ahead is "on notice",
    # and the monthly strip shows it as a planned exit in its own month.
    exits = [e for e in emps if e["exit_on"] and period.contains(e["exit_on"]) and e["exit_on"] <= today]
    opening = sum(1 for e in emps if _on_rolls(e, period.start))
    avg_headcount = (opening + len(current if period.contains(today) else
                                   [e for e in emps if _on_rolls(e, period.end)])) / 2.0
    notice = [e for e in current if e["resigned"] and (not e["exit_on"] or e["exit_on"] >= today)]

    # Who was deployed on a given day, from the assignment rows the Revenue
    # report costs (start / exit) — `deployment_by_employee` only knows TODAY.
    assignments = _deployed_rows(db)

    def _deployed_on(on: date) -> set[int]:
        return {r["employee_id"] for r in assignments
                if (not r["start"] or r["start"] <= on) and (not r["exit"] or r["exit"] >= on)}

    monthly = []
    for m in _months_in(period):
        if m.start <= today:
            as_of = min(m.end, today)
            on_rolls = [e for e in emps if _on_rolls(e, as_of)]
            dep = _deployed_on(as_of)
            m_bench = [e for e in on_rolls if e["id"] not in dep]
            headcount, deployed_n = len(on_rolls), len(on_rolls) - len(m_bench)
            cost = round(sum(e["ctc"] / 12.0 for e in on_rolls if e["ctc"] is not None), 2)
            m_bench_cost = round(sum(e["ctc"] / 12.0 for e in m_bench if e["ctc"] is not None), 2)
        else:
            headcount = deployed_n = cost = m_bench_cost = None
        monthly.append({
            "key": m.key, "label": m.start.strftime("%b %y"),
            "joiners": sum(1 for e in emps if e["doj"] and m.start <= e["doj"] <= m.end),
            "exits": sum(1 for e in emps if e["exit_on"] and m.start <= e["exit_on"] <= m.end),
            "headcount": headcount, "deployed": deployed_n,
            "bench": None if headcount is None else headcount - deployed_n,
            "cost": cost, "bench_cost": m_bench_cost,
            "future": m.start > today,
        })

    tenure_years = [((today - e["doj"]).days / 365.25) for e in current if e["doj"]]
    invoices = _invoices_in_window(db, period.start, period.end)
    billed = sum(i["sub_total"] for i in invoices)
    rolloffs = bench_rolloffs(db, days=ROLLOFF_DAYS)
    people_cost_month = sum(e["ctc"] / 12.0 for e in current if e["ctc"] is not None)
    bench_ids = {e["id"] for e in bench}
    notice_ids = {e["id"] for e in notice}

    # Deployed vs bench by role — the skills sitting idle, by name.
    by_role: dict[str, dict] = {}
    for e in current:
        slot = by_role.setdefault(e["role"] or "No designation",
                                  {"role": e["role"] or "No designation", "deployed": 0, "bench": 0, "on_notice": 0, "cost": 0.0})
        slot["deployed" if e["id"] not in bench_ids else "bench"] += 1
        slot["on_notice"] += 1 if e["id"] in notice_ids else 0
        slot["cost"] += e["ctc"] / 12.0 if e["ctc"] is not None else 0.0
    for slot in by_role.values():
        slot["cost"] = round(slot["cost"], 2)

    # Tenure mix — how long the people we have today have been with us.
    tenure = [{"label": label, "deployed": 0, "bench": 0} for _, label in TENURE_BUCKETS]
    for e in current:
        if not e["doj"]:
            continue
        years = (today - e["doj"]).days / 365.25
        idx = next(i for i, (cap, _) in enumerate(TENURE_BUCKETS) if cap is None or years < cap)
        tenure[idx]["deployed" if e["id"] not in bench_ids else "bench"] += 1

    # Roll-offs by month — how many heads need a next assignment, and when.
    rate_by_pe = {r["pe_id"]: _monthly_rate(r["rate"], r["unit"]) for r in assignments}
    rolloffs_by_month: dict[str, dict] = {}
    for r in rolloffs:
        if not r["po_end_date"]:
            continue
        end = date.fromisoformat(r["po_end_date"])
        key = f"{end.year:04d}-{end.month:02d}"
        slot = rolloffs_by_month.setdefault(key, {"key": key, "label": end.strftime("%b %y"), "heads": 0, "monthly_rate": 0.0})
        slot["heads"] += 1
        slot["monthly_rate"] += rate_by_pe.get(r["project_employee_id"], 0.0)
    rolloff_months = [dict(v, monthly_rate=round(v["monthly_rate"], 2)) for _, v in sorted(rolloffs_by_month.items())]

    exits_n, joiners_n = len(exits), len(joiners)
    attrition = _pct(exits_n, avg_headcount)
    bench_cost_pct = _pct(bench_cost, people_cost_month)
    heads_without_ctc = sum(1 for e in current if e["ctc"] is None)
    alerts: list[dict] = []
    if bench_cost_pct is not None and bench_cost_pct >= BENCH_COST_WARN_PCT:
        _alert(alerts, "bench_cost", "bad" if bench_cost_pct >= 2 * BENCH_COST_WARN_PCT else "warn",
               f"Bench costs ₹{bench_cost:,.0f} a month ({bench_cost_pct:.0f}% of people cost)",
               f"{len(bench)} head(s) with no live assignment today.")
    soon = [r for r in rolloffs if r["days_left"] is not None and r["days_left"] <= 30]
    if soon:
        _alert(alerts, "rolloffs_30", "warn", f"{len(soon)} roll-off(s) within 30 days",
               "Cover ends with the project's last working day or its PO — line up the next assignment.")
    if attrition is not None and attrition >= ATTRITION_WARN_PCT:
        _alert(alerts, "attrition", "bad", f"Attrition at {attrition:.0f}%",
               f"{exits_n} left against an average headcount of {avg_headcount:.0f}.")
    if notice:
        _alert(alerts, "on_notice", "warn", f"{len(notice)} serving notice",
               "Backfill or redeploy before the last working day: " + ", ".join(e["name"] for e in notice[:3]) + ".")
    if heads_without_ctc:
        _alert(alerts, "no_ctc", "warn", f"{heads_without_ctc} employee(s) without a CTC",
               "Every cost and margin on this page is understated until HR fills it in.")

    def _emp_view(e: dict) -> dict:
        return {"employee_id": e["id"], "employee": e["name"], "employee_code": e["code"],
                "role": e["role"], "doj": e["doj"].isoformat() if e["doj"] else None,
                "cost_month": round(e["ctc"] / 12.0, 2) if e["ctc"] is not None else None,
                "has_ctc": e["ctc"] is not None,
                "exit_on": e["exit_on"].isoformat() if e["exit_on"] else None,
                "projects": live.get(e["id"], {}).get("projects", [])}

    return {
        "headline": {
            "headcount": len(current), "deployed": len(deployed), "bench": len(bench),
            "utilisation_pct": _pct(len(deployed), len(current)),
            "bench_cost_month": round(bench_cost, 2),
            "people_cost_month": round(people_cost_month, 2),
            "bench_cost_pct": bench_cost_pct,
            "heads_without_ctc": heads_without_ctc,
            "joiners": joiners_n, "exits": exits_n,
            "net_change": joiners_n - exits_n,
            "opening_headcount": opening,
            "attrition_pct": attrition,
            "on_notice": len(notice),
            "avg_tenure_years": round(sum(tenure_years) / len(tenure_years), 1) if tenure_years else None,
            "billed": round(billed, 2),
            "revenue_per_deployed_head": round(billed / len(deployed), 2) if deployed else None,
            "rolloffs_90d": len(rolloffs),
        },
        "monthly": monthly,
        "by_role": sorted(by_role.values(), key=lambda r: (-(r["deployed"] + r["bench"]), r["role"])),
        "tenure": tenure,
        "rolloffs_by_month": rolloff_months,
        "alerts": alerts,
        "bench": sorted((_emp_view(e) for e in bench), key=lambda e: -(e["cost_month"] or 0)),
        "on_notice": sorted((_emp_view(e) for e in notice), key=lambda e: e["exit_on"] or "9999"),
        "joiners": sorted((_emp_view(e) for e in joiners), key=lambda e: e["doj"] or "", reverse=True),
        "exits": sorted((_emp_view(e) for e in exits), key=lambda e: e["exit_on"] or "", reverse=True),
        "rolloffs": rolloffs,
        "placements": _people_placements(db, period, today),
    }


def _people_placements(db: Session, period: Period, today: date) -> dict | None:
    """Internal vs external placements for the People tab (29 Sep 2026, user ask:
    "on the HR Dashboard, how many employees were placed internal or external,
    as bars"). The SAME `placements_report` the Revenue page uses — same window,
    same rule — with the money taken out: HR reads this through
    `/api/dashboard/people`, and billing is Admin / CEO's. Never raises."""
    try:
        rep = placements_report(db, month_raw=period.anchor.key, period_raw=period.kind, today=today)
    except Exception:  # pragma: no cover — a failing panel must not blank the tab
        logger.warning("people placements failed", exc_info=True)
        return None
    money = ("billed",)
    return {
        "window": rep["window"],
        "headline": rep["headline"],
        "series": rep["series"],
        "by_customer": [{k: v for k, v in row.items() if not k.startswith("billed_")}
                        for row in rep["by_customer"]],
        "rows": [{k: v for k, v in row.items() if k not in money} for row in rep["rows"]],
        "rows_truncated": rep["rows_truncated"],
        "rules": {k: v for k, v in rep["rules"].items() if k != "revenue"},
    }


# --------------------------------------------------------------------------- entry point


_BUILDERS = {"finance": _finance, "customer": _customer, "sales": _sales, "people": _people}


def executive_dashboard(db: Session, tab: str, month_raw: str | None = None,
                        period_raw: str | None = None, today: date | None = None) -> dict:
    """One tab of the CEO dashboard. `tab` ∈ TABS; `month` is the anchor and
    `period` the zoom (default FY). Raises ValueError on a bad tab / period."""
    tab = (tab or "").strip().lower()
    if tab not in _BUILDERS:
        raise ValueError(f"Unknown tab {tab!r}; expected one of {', '.join(TABS)}")
    today = today or date.today()
    period = Period.parse(period_raw or DEFAULT_PERIOD, month_raw, today)
    return {
        "as_of": today.isoformat(),
        "tab": tab,
        "period": _period_view(period, today),
        "data": _BUILDERS[tab](db, period, today),
    }
