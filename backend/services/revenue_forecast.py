"""Predicted revenue (4 Nov 2026).

User ask: "predicted revenue with a graph — with customer count and project
count — month, quarter and year on the Dashboard", fed by the planned release /
extension dates Sales Managers record (services/pe_release).

The prediction is the deployed team × each person's monthly rate, prorated for
onboarding and for the PLANNED end (the earlier of the exit date and the
release date the customer gave). New hires are not assumed. Everything here is
PURE over the rows `revenue_report._deployed_rows` returns, so the dashboard,
the drill page and the Revenue report's 3-month forecast share one rule.
"""
from __future__ import annotations

from datetime import date

from services.revenue_report import (
    Month, Period, _monthly_rate, _overlap_fraction, plan_end,
)

#: How many buckets ahead each zoom shows (the current bucket included).
AHEAD = {"month": 12, "quarter": 4, "fy": 2}


def month_prediction(deployed: list[dict], m: Month) -> dict:
    """Predicted billing of one calendar month. PURE."""
    amount = 0.0
    without_release = 0.0
    people, customers, projects, releasing = set(), set(), set(), []
    for d in deployed:
        rate = _monthly_rate(d["rate"], d["unit"])
        frac = _overlap_fraction(d["start"], plan_end(d), m)
        base = _overlap_fraction(d["start"], d.get("exit"), m)
        without_release += rate * base
        rel = d.get("release")
        if rel and m.start <= rel <= m.end and (not d.get("exit") or rel < d["exit"]):
            releasing.append(d)
        if frac <= 0:
            continue
        amount += rate * frac
        people.add(d.get("pe_id"))
        if d.get("customer_id"):
            customers.add(d["customer_id"])
        if d.get("project_id"):
            projects.add(d["project_id"])
    return {"amount": amount, "without_release": without_release, "people": people,
            "customers": customers, "projects": projects, "releasing": releasing}


def _bucket(deployed: list[dict], period: Period, today: date, billed_by_month: dict) -> dict:
    amount = without = billed = 0.0
    people, customers, projects = set(), set(), set()
    releasing = 0
    for i in range(period.months):
        m = period.first_month.shift(i)
        p = month_prediction(deployed, m)
        amount += p["amount"]
        without += p["without_release"]
        people |= p["people"]
        customers |= p["customers"]
        projects |= p["projects"]
        releasing += len(p["releasing"])
        billed += billed_by_month.get(m.key, 0.0)
    return {
        "key": period.key, "label": period.short_label, "kind": period.kind,
        "start": period.start.isoformat(), "end": period.end.isoformat(),
        "predicted": round(amount, 2),
        "lost_to_releases": round(max(0.0, without - amount), 2),
        "billed_so_far": round(billed, 2),
        "heads": len(people), "customers": len(customers), "projects": len(projects),
        "releasing": releasing,
        "current": period.contains(today),
    }


def predicted_trend(deployed: list[dict], today: date,
                    billed_by_month: dict | None = None) -> dict:
    """{month: [12], quarter: [4], fy: [2]} buckets from the current one on,
    plus the planned releases coming up. PURE."""
    billed_by_month = billed_by_month or {}
    anchor = Month(today.year, today.month)
    out = {}
    for kind, n in AHEAD.items():
        first = Period(kind, anchor)
        out[kind] = [_bucket(deployed, first.shift(i), today, billed_by_month) for i in range(n)]
    upcoming = sorted(
        ({"pe_id": d.get("pe_id"), "name": d.get("name"), "customer": d.get("customer"),
          "customer_id": d.get("customer_id"), "project": d.get("project"),
          "project_id": d.get("project_id"), "release_date": d["release"].isoformat(),
          "days_left": (d["release"] - today).days,
          "monthly_rate": round(_monthly_rate(d["rate"], d["unit"]), 2)}
         for d in deployed
         if d.get("release") and d["release"] >= today
         and (not d.get("exit") or d["release"] < d["exit"])),
        key=lambda r: r["release_date"])
    return {
        **out,
        "releases": upcoming,
        "assumptions": ("Deployed team × monthly rate (Daily × 21, Hourly × 21 × 8, Yearly ÷ 12), "
                        "prorated for onboarding, exit and the planned release dates Sales record. "
                        "New hires are not assumed."),
    }
