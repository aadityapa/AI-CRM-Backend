"""Sub-tab grants (22 Oct 2026): a template / custom role ticks the sub-tabs each
page opens — e.g. Finance gets only Reports ▸ Financial Reports + Revenue.

Rule (`access_registry.subtab_choice`): nothing set to "view" for a tab → the
role decides and only an explicit "hidden" hides; any sub-tab of the tab set to
"view" → the template decides that tab's sub-tabs alone.
"""
from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import crm_deps
from services.access_registry import (
    FIELDS_BY_TAB, SUB_TABS, registry, subtab_choice, validate_access,
)


def test_choice_follows_the_role_until_a_list_is_picked():
    assert subtab_choice(None, "revenue") == "default"
    assert subtab_choice({"export": "view"}, "revenue") == "default"          # a field, not a sub-tab
    assert subtab_choice({"tab:opportunities": "hidden"}, "revenue") == "default"
    assert subtab_choice({"tab:opportunities": "hidden"}, "opportunities") == "hide"


def test_a_picked_list_decides_alone():
    finance_only = {"tab:financial": "view", "tab:revenue": "view",
                    "tab:opportunities": "hidden", "tab:profiles": "hidden"}
    assert subtab_choice(finance_only, "revenue") == "show"
    assert subtab_choice(finance_only, "financial") == "show"
    assert subtab_choice(finance_only, "opportunities") == "hide"
    # Not mentioned in a picked list → hidden (a new sub-tab never leaks in).
    assert subtab_choice(finance_only, "productivity") == "hide"


def test_registry_lists_every_reports_sub_tab_and_keeps_them_out_of_fields():
    assert set(SUB_TABS["reports"]) == {"opportunities", "profiles", "productivity", "financial", "revenue"}
    assert "tab:revenue" in FIELDS_BY_TAB["reports"]
    reports = next(t for t in registry()["tabs"] if t["key"] == "reports")
    assert {s["key"] for s in reports["subtabs"]} >= {"tab:financial", "tab:revenue"}
    assert all(not f["key"].startswith("tab:") for f in reports["fields"])
    # A template saving a ticked list validates.
    validate_access({"reports": "view"}, {"reports": {"tab:revenue": "view", "tab:profiles": "hidden"}})


@pytest.mark.parametrize("tab", ["customers", "branch-policy", "candidates", "projects",
                                 "project-employees", "timesheets", "invoices", "employees",
                                 "talent-pool", "finance-reports"])
def test_pages_with_inner_tabs_are_grantable(tab):
    assert SUB_TABS.get(tab), tab


def _user(admin=False):
    return SimpleNamespace(id=7, roles={"Admin"} if admin else {"GM"}, is_admin=admin)


def _refuse(user, db=None):
    raise HTTPException(status_code=403, detail="base refused")


def _admit(user, db=None):
    return user


def test_gate_opens_shown_closes_hidden_and_defers_otherwise(monkeypatch):
    gate = crm_deps.subtab_gate("reports", ("revenue", "financial"), _refuse)
    monkeypatch.setattr(crm_deps, "subtab_choice_for", lambda db, user, tab, sub: "show" if sub == "financial" else "hide")
    assert gate(_user(), None).id == 7                     # one shown sub-tab opens the route

    monkeypatch.setattr(crm_deps, "subtab_choice_for", lambda *a: "hide")
    with pytest.raises(HTTPException) as e:
        crm_deps.subtab_gate("reports", "revenue", _admit)(_user(), None)
    assert e.value.status_code == 403 and "Revenue" in e.value.detail

    monkeypatch.setattr(crm_deps, "subtab_choice_for", lambda *a: "default")
    with pytest.raises(HTTPException):
        crm_deps.subtab_gate("reports", "revenue", _refuse)(_user(), None)
    assert crm_deps.subtab_gate("reports", "revenue", _admit)(_user(), None).id == 7


def test_report_routes_are_wired_to_their_sub_tabs():
    from routers.crm import finance_reports, reports
    assert "OPP_REPORT_READ" in inspect.getsource(reports.opportunities_report) \
        or "OPP_REPORT_READ" in inspect.getsource(reports)
    assert "REVENUE_READ" in inspect.getsource(reports.revenue)
    src = inspect.getsource(finance_reports)
    assert 'LEDGER_READ = subtab_gate("finance-reports", "ledger", _FIN_REPORTS)' in src
    assert "Depends(LEDGER_READ)" in inspect.getsource(finance_reports.customer_ledger)
