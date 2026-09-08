"""The role-default template matrix must stay valid against the registry.

A tab renamed or removed from access_registry.TABS would otherwise silently
orphan grants in the seeded templates (_strip_removed_keys drops unknown keys
on save — no error, no grant). Run: python -m pytest tests/test_role_template_defaults.py -q
"""
from __future__ import annotations

from services.access_registry import MODES, TABS
from services.role_template_defaults import ROLE_TEMPLATE_DEFAULTS


def test_every_granted_tab_exists_in_the_registry():
    for role, tabs in ROLE_TEMPLATE_DEFAULTS.items():
        unknown = [t for t in tabs if t not in TABS]
        assert unknown == [], f"{role}: unknown tab keys {unknown}"


def test_every_mode_is_on_the_ladder():
    for role, tabs in ROLE_TEMPLATE_DEFAULTS.items():
        bad = {t: m for t, m in tabs.items() if m not in MODES}
        assert bad == {}, f"{role}: invalid modes {bad}"


def test_all_six_crm_roles_covered():
    assert set(ROLE_TEMPLATE_DEFAULTS) == {
        "Sales", "Sales_Head", "RMG", "TA", "HR", "Finance"}


def test_core_workflow_grants_hold():
    """Pin the grants each role's daily work depends on — a future edit that
    drops one of these locks a whole team out of their main screen."""
    d = ROLE_TEMPLATE_DEFAULTS
    assert d["TA"]["requirements"] == "edit"          # sourcing is TA's job
    assert d["TA"]["profiles"] == "create"            # TA applies candidates
    assert d["RMG"]["template-requests"] == "edit"    # RMG fulfils templates
    assert d["Sales"]["opportunities"] == "create"    # Sales creates deals
    assert d["Finance"]["invoices"] == "create"       # Finance bills
    assert d["HR"]["employees"] == "create"           # HR owns the directory
    for role in d:
        assert d[role].get("dashboard") == "view"     # everyone lands somewhere
