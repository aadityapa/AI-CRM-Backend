"""Access Control step 1 (7 Oct 2026): audit log, safe delete, admin guards,
forced password change, refresh re-check.

Run:  cd backend && python -m pytest tests/test_access_safety.py -q
"""
from __future__ import annotations

import importlib
import sqlite3
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.dialects.postgresql import ARRAY, INET, JSONB, UUID
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool


@compiles(JSONB, "sqlite")
def _j(e, c, **k):  # noqa: ANN001
    return "JSON"


@compiles(ARRAY, "sqlite")
def _a(e, c, **k):  # noqa: ANN001
    return "JSON"


@compiles(UUID, "sqlite")
def _u(e, c, **k):  # noqa: ANN001
    return "VARCHAR(36)"


@compiles(INET, "sqlite")
def _i(e, c, **k):  # noqa: ANN001
    return "VARCHAR(64)"


for _m in [
    "base", "rbac", "customers", "opportunities", "projects", "leave", "timesheets",
    "finance", "hr", "candidates", "masters", "requirements", "profiles", "resumes",
    "ai_links", "scheduling", "user_profiles", "template_requests", "access_templates",
    "custom_roles", "user_access_log",
]:
    importlib.import_module(f"models.{_m}")

import crm_deps  # noqa: E402
from models.base import Base  # noqa: E402
from models import Customer, Opportunity, OppType, Role, RoleName, UserRole  # noqa: E402
from models.user_access_log import UserAccessLog  # noqa: E402
from services import access_audit as audit  # noqa: E402
from services import users_admin as svc  # noqa: E402

ADMIN = SimpleNamespace(id=1, full_name="Karan Singh", username="karan")


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    s = Session(bind=engine, future=True)
    # The ORM maps registration_data as a one-column FK stub; give it the real
    # columns the user-admin code reads.
    for ddl in ("full_name TEXT", "email TEXT", "username TEXT", "role TEXT DEFAULT 'hr'",
                "is_active INTEGER NOT NULL DEFAULT 1", "must_change_password INTEGER NOT NULL DEFAULT 0",
                "password_hash TEXT", "password_salt TEXT"):
        s.execute(text(f"ALTER TABLE registration_data ADD COLUMN {ddl}"))
    for uid, name in ((1, "Karan Singh"), (2, "Balasaheb S"), (3, "Gargee Joshi"), (4, "Spare Account")):
        s.execute(text("INSERT INTO registration_data (id, full_name, email, username) VALUES (:i, :n, :e, :u)"),
                  {"i": uid, "n": name, "e": f"u{uid}@karnex.in", "u": f"u{uid}"})
    admin = Role(name=RoleName.ADMIN)
    sales = Role(name=RoleName.SALES)
    s.add_all([admin, sales]); s.flush()
    s.add_all([UserRole(user_id=1, role_id=admin.id), UserRole(user_id=2, role_id=sales.id),
               UserRole(user_id=3, role_id=sales.id)])
    s.commit()
    try:
        yield s
    finally:
        s.close()


def _make_admin(db, uid):
    admin = db.query(Role).filter_by(name=RoleName.ADMIN).one()
    db.add(UserRole(user_id=uid, role_id=admin.id)); db.commit()


# ------------------------------------------------------------------ audit

def test_record_writes_a_row_with_a_readable_diff(db):
    before = audit.snapshot(db, 2)
    svc.replace_roles(db, 2, ["Sales", "TA"], actor_id=1)
    audit.record(db, actor=ADMIN, action="user.roles", target_user_id=2, before=before,
                 after=audit.snapshot(db, 2))
    row = db.query(UserAccessLog).one()
    assert row.actor_name == "Karan Singh" and row.target_name == "Balasaheb S"
    assert row.summary == "Roles: Sales → Sales, TA"
    rows, meta = audit.history(db, user_id=2)
    assert meta["total"] == 1 and rows[0]["label"] == "Roles changed" and rows[0]["group"] == "access"


def test_history_filters_by_group_and_search_and_rejects_unknown_groups(db):
    audit.record(db, actor=ADMIN, action="user.password_reset", target_user_id=3, summary="Temporary password")
    audit.record(db, actor=ADMIN, action="role.created", subject_type="role", subject_name="GM",
                 summary="Role 'GM' created")
    assert audit.history(db, group="security")[1]["total"] == 1
    assert audit.history(db, search="gm")[0][0]["subject_name"] == "GM"
    with pytest.raises(HTTPException):
        audit.history(db, group="nope")


def test_record_never_raises(db):
    audit.record(db, actor=None, action="x" * 200, target_user_id=999999)   # unknown user, long key
    assert db.query(UserAccessLog).count() == 1


# ------------------------------------------------------------------ delete

def test_a_user_with_history_is_never_deleted(db):
    cust = Customer(name="HARMAN"); db.add(cust); db.flush()
    db.add(Opportunity(opp_id="C-1", title="Deal", customer_id=cust.id, opp_type=OppType.T_AND_M, created_by=2))
    db.commit()
    with pytest.raises(HTTPException) as e:
        svc.delete_user(db, 2, actor_id=1)
    assert e.value.status_code == 409 and "1 opportunities created" in e.value.detail
    # Nothing was rewritten: the deal still belongs to its Sales owner.
    assert db.query(Opportunity).one().created_by == 2
    assert db.execute(text("SELECT COUNT(*) FROM registration_data WHERE id = 2")).scalar() == 1


def test_an_unused_account_deletes_cleanly(db):
    out = svc.delete_user(db, 4, actor_id=1)
    assert out["id"] == 4
    assert db.execute(text("SELECT COUNT(*) FROM registration_data WHERE id = 4")).scalar() == 0


def test_you_cannot_delete_yourself_or_the_last_admin(db):
    with pytest.raises(HTTPException):
        svc.delete_user(db, 1, actor_id=1)
    with pytest.raises(HTTPException) as e:
        svc.delete_user(db, 1, actor_id=2)
    assert "last active Admin" in e.value.detail


# ------------------------------------------------------------------ guards

def test_deactivating_needs_a_reason_and_never_yourself(db):
    with pytest.raises(HTTPException) as e:
        svc.set_user_active(db, 2, False, actor_id=1, reason="")
    assert "reason" in e.value.detail
    with pytest.raises(HTTPException):
        svc.set_user_active(db, 1, False, actor_id=1, reason="leaving the company")
    out = svc.set_user_active(db, 2, False, actor_id=1, reason="Left the company")
    assert out["is_active"] is False
    svc.set_user_active(db, 2, True, actor_id=1)        # activating needs no reason


def test_the_last_admin_cannot_be_deactivated_or_demoted(db):
    with pytest.raises(HTTPException) as e:
        svc.set_user_active(db, 1, False, actor_id=2, reason="test the guard")
    assert "last active Admin" in e.value.detail
    with pytest.raises(HTTPException) as e:
        svc.replace_roles(db, 1, ["Sales"], actor_id=2)
    assert "last active Admin" in e.value.detail
    _make_admin(db, 3)
    with pytest.raises(HTTPException) as e:                     # never your own admin role
        svc.replace_roles(db, 1, ["Sales"], actor_id=1)
    assert "your own" in e.value.detail
    svc.replace_roles(db, 1, ["Sales"], actor_id=3)              # another admin remains → allowed


def test_open_work_lists_live_deals(db):
    cust = Customer(name="HARMAN"); db.add(cust); db.flush()
    db.add(Opportunity(opp_id="C-2", title="Deal", customer_id=cust.id, opp_type=OppType.T_AND_M, created_by=3))
    db.commit()
    work = {w["key"]: w["count"] for w in svc.open_work(db, 3)}
    assert work.get("deals") == 1


# ------------------------------------------------------------------ password

def test_an_admin_reset_forces_a_change_at_next_sign_in(db, monkeypatch):
    calls = {}
    import auth_db
    monkeypatch.setattr(svc, "_legacy_db_target", lambda: "x")
    monkeypatch.setattr(auth_db, "update_user_password", lambda *a, **k: None)
    monkeypatch.setattr(auth_db, "set_must_change_password",
                        lambda target, uid, value: calls.setdefault("flag", (uid, value)))
    out = svc.reset_password(db, 2, None, actor_id=1)
    assert out["generated"] and calls["flag"] == (2, True)


def test_a_flagged_user_can_only_reach_the_change_password_screen(db, monkeypatch):
    db.execute(text("UPDATE registration_data SET must_change_password = 1 WHERE id = 2"))
    db.commit()
    monkeypatch.setattr(crm_deps, "_decode_bearer", lambda request: {"sub": "u2"})
    crm_deps._MUST_CHANGE_PROBE.update({"has": None, "at": 0.0})

    def req(path):
        return SimpleNamespace(url=SimpleNamespace(path=path), headers={})
    with pytest.raises(HTTPException) as e:
        crm_deps.get_current_user(req("/api/opportunities"), db)
    assert e.value.status_code == 403 and crm_deps.PASSWORD_CHANGE_REQUIRED in e.value.detail
    user = crm_deps.get_current_user(req("/api/me"), db)
    assert user.must_change_password is True
    assert crm_deps.get_current_user(req("/api/me/change-password"), db).id == 2


def test_refresh_reads_the_account_state(tmp_path):
    from auth_db import account_state
    path = tmp_path / "auth.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE registration_data (id INTEGER PRIMARY KEY, username TEXT, is_active INTEGER)")
    con.execute("INSERT INTO registration_data VALUES (1, 'live', 1), (2, 'gone', 0)")
    con.commit(); con.close()
    assert account_state(str(path), "live") == {"id": 1, "is_active": True}
    assert account_state(str(path), "GONE")["is_active"] is False
    assert account_state(str(path), "nobody") is None


# ------------------------------------------------------------------ the list

def test_the_users_list_filters_and_counts(db):
    from crm_deps import page_params
    db.execute(text("UPDATE registration_data SET role = 'hr'"))
    db.execute(text("UPDATE registration_data SET is_active = 0 WHERE id = 3"))
    db.execute(text("UPDATE registration_data SET must_change_password = 1 WHERE id = 2"))
    db.commit()
    rows, meta = svc.list_users(db, page_params())
    assert meta["counts"] == {"total": 4, "active": 3, "inactive": 1, "no_role": 1, "password_pending": 1}
    by_id = {r["id"]: r for r in rows}
    assert by_id[2]["must_change_password"] is True and by_id[1]["must_change_password"] is False
    assert [r["id"] for r in svc.list_users(db, page_params(), status="inactive")[0]] == [3]
    assert [r["id"] for r in svc.list_users(db, page_params(), status="no_role")[0]] == [4]
    assert [r["id"] for r in svc.list_users(db, page_params(), role="Sales")[0]] == [2, 3]
    assert svc.list_users(db, page_params(search="gargee"))[1]["total"] == 1
    with pytest.raises(HTTPException):
        svc.list_users(db, page_params(), status="bogus")
