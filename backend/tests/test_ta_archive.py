"""TA archives closed candidacies only (6 Oct 2026, user ask)."""
from types import SimpleNamespace

import routers.crm.candidate_profiles as cp


def _u(*roles, admin=False):
    return SimpleNamespace(id=7, roles=list(roles), is_admin=admin)


def test_ta_only_login_is_limited_to_closed_candidacies(monkeypatch):
    import services.action_permissions as ap
    monkeypatch.setattr(ap, "screens_as_rmg", lambda db, user: False)
    assert cp.archive_closed_only(None, _u("TA")) is True
    for other in ("RMG", "Sales", "Sales_Head"):
        assert cp.archive_closed_only(None, _u("TA", other)) is False
    assert cp.archive_closed_only(None, _u("Admin", admin=True)) is False
    assert cp.archive_closed_only(None, _u("RMG")) is False


def test_a_ta_who_screens_as_rmg_keeps_any_stage(monkeypatch):
    import services.action_permissions as ap
    monkeypatch.setattr(ap, "screens_as_rmg", lambda db, user: True)
    assert cp.archive_closed_only(None, _u("TA", "GM")) is False


def test_gate_admits_ta_and_route_checks_the_stage():
    import inspect
    src = inspect.getsource(cp)
    assert 'gated_write("profiles", "RMG", "Sales", "Sales_Head", "TA")' in src
    route = inspect.getsource(cp.archive_applied_candidate)
    assert "archive_closed_only" in route and "REJECTED_BUCKET" in route
    assert 'rmg_screening_status' in route  # an RMG screening rejection counts too
