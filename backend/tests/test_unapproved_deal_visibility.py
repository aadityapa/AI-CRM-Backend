"""7 Oct 2026 — a deal waiting for the Sales Head is Sales' business only.

RMG / GM / TA saw C-2026-00102 / 00103 (Pending Sales Head approval) on their
Opportunities list. `sees_unapproved_deals` decides, and both the list and the
detail route apply it. Applied Candidates rows also carry the Screening Desk's
extras so RMG / GM can act there without the desk."""
from __future__ import annotations

import inspect
from types import SimpleNamespace

from services.requirements import approved_deals_clause, sees_unapproved_deals


def _user(*roles, admin=False):
    return SimpleNamespace(is_admin=admin, has_any=lambda *r: bool(set(r) & set(roles)))


def test_only_sales_and_admins_see_unapproved_deals():
    assert sees_unapproved_deals(_user("Sales"))
    assert sees_unapproved_deals(_user("Sales_Head"))
    assert sees_unapproved_deals(_user(admin=True))
    assert not sees_unapproved_deals(_user("RMG"))
    assert not sees_unapproved_deals(_user("TA"))
    assert not sees_unapproved_deals(_user("GM"))


def test_the_clause_names_the_approved_status():
    assert "approval_status" in str(approved_deals_clause())


def test_list_and_detail_both_apply_the_rule():
    from routers.crm import opportunities
    assert "approved_deals_clause()" in inspect.getsource(opportunities.list_opportunities)
    assert "sees_unapproved_deals" in inspect.getsource(opportunities.get_opportunity)


def test_applied_candidates_rows_carry_the_desk_extras():
    from routers.crm import resumes
    src = inspect.getsource(resumes._with_screening_extras)
    for key in ("direct_to_sales_block", "internal_employee", "fast_track_block", "new_results"):
        assert f'row["{key}"]' in src
