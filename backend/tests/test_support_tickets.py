"""Help & Support (14 Sep 2026): bot escalation marker, tickets raised by any
logged-in user, Admin/CEO see & act on all, notifications on both sides."""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.dialects.postgresql import ARRAY, INET, JSONB, UUID
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


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


for _m in ["base", "rbac", "support"]:
    importlib.import_module(f"models.{_m}")

import crm_deps  # noqa: E402
from models import Base  # noqa: E402
from models.base import users_table_stub  # noqa: E402
from services import support as svc  # noqa: E402
import routers.crm.support as support_router  # noqa: E402

USER = crm_deps.CurrentUser(id=2, username="ta1", full_name="Mohammed Suhel", roles={"TA"})
OTHER = crm_deps.CurrentUser(id=3, username="hr1", full_name="HR One", roles={"HR"})
ADMIN = crm_deps.CurrentUser(id=1, username="karan", full_name="Karan Singh", roles={"CEO"})


@pytest.fixture()
def world(monkeypatch):
    sent: list[dict] = []
    import services.notify as notify
    monkeypatch.setattr(notify, "notify_roles",
                        lambda db, roles, title, message="", link="", **kw: sent.append(
                            {"roles": list(roles), "title": title, "link": link, "event": kw.get("event")}) or len(roles))
    monkeypatch.setattr(notify, "notify_user",
                        lambda db, uid, title, message="", link="", **kw: sent.append(
                            {"user": uid, "title": title, "link": link, "event": kw.get("event")}))
    engine = sa.create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    s = Session(bind=engine, future=True)
    for uid in (1, 2, 3):
        s.execute(users_table_stub.insert().values(id=uid))
    s.commit()
    app = FastAPI()
    app.include_router(support_router.router)
    app.dependency_overrides[crm_deps.get_crm_db] = lambda: s
    current = {"user": USER}
    app.dependency_overrides[crm_deps.get_current_user] = lambda: current["user"]
    client = TestClient(app)

    def as_(u):
        current["user"] = u
    yield client, as_, sent, s
    s.close()


def _raise(client, **over):
    body = {"subject": "Profiles filter not working", "description": "Applied date filter shows all rows still.",
            "category": "Bug", "priority": "High", "page": "profiles",
            "bot_transcript": [{"role": "user", "content": "filter broken"},
                               {"role": "assistant", "content": "Try refresh"}]}
    body.update(over)
    return client.post("/api/support/tickets", json=body)


def test_user_raises_ticket_and_admins_are_notified(world):
    client, as_, sent, _ = world
    r = _raise(client)
    assert r.status_code == 200, r.text
    t = r.json()["data"]
    assert t["ticket_no"] == f"T-{t['id']:04d}" and t["status"] == "Open" and t["user_name"] == "Mohammed Suhel"
    assert t["user_roles"] == "TA" and t["bot_transcript"][0]["content"] == "filter broken"
    assert t["messages"][0]["body"].startswith("Applied date filter")
    assert sent and sent[0]["roles"] == ["Admin", "CEO"] and sent[0]["event"] == "support.ticket_raised"
    assert sent[0]["link"].endswith(f"support-tickets/{t['id']}")
    assert t["ticket_no"] in sent[0]["title"]


def test_ticket_validation(world):
    client, *_ = world
    assert _raise(client, category="Nope").status_code == 400
    assert _raise(client, priority="Whatever").status_code == 400
    assert _raise(client, description="short").status_code == 422


def test_only_owner_or_staff_can_see_a_ticket(world):
    client, as_, _, _ = world
    tid = _raise(client).json()["data"]["id"]
    as_(OTHER)
    assert client.get(f"/api/support/tickets/{tid}").status_code == 404      # never leaks
    assert client.get("/api/support/tickets").json()["data"] == []
    as_(ADMIN)
    assert client.get(f"/api/support/tickets/{tid}").status_code == 200
    assert len(client.get("/api/support/tickets").json()["data"]) == 1
    as_(USER)
    assert client.get(f"/api/support/tickets/{tid}").status_code == 200


def test_admin_reply_moves_to_in_progress_and_notifies_user(world):
    client, as_, sent, _ = world
    tid = _raise(client).json()["data"]["id"]
    sent.clear()
    as_(ADMIN)
    r = client.post(f"/api/support/tickets/{tid}/messages", json={"body": "Looking into it — which TA did you pick?"})
    assert r.status_code == 200
    t = r.json()["data"]
    assert t["status"] == "In_Progress"
    staff = [m for m in t["messages"] if m["is_staff"] and not m["is_system"]]
    assert staff and staff[-1]["author_name"] == "Karan Singh"
    assert any(m["is_system"] and "Open → In Progress" in m["body"] for m in t["messages"])
    assert any(n.get("user") == 2 and n["event"] == "support.ticket_replied" for n in sent)


def test_resolve_then_user_reply_reopens(world):
    client, as_, sent, _ = world
    tid = _raise(client).json()["data"]["id"]
    as_(ADMIN)
    r = client.patch(f"/api/support/tickets/{tid}", json={"status": "Resolved", "note": "Fixed in today's build"})
    assert r.status_code == 200 and r.json()["data"]["status"] == "Resolved"
    assert r.json()["data"]["resolved_at"]
    assert any(n.get("user") == 2 and n["event"] == "support.ticket_status" for n in sent)
    as_(USER)
    assert client.post(f"/api/support/tickets/{tid}/rating", json={"rating": 5}).status_code == 200
    r = client.post(f"/api/support/tickets/{tid}/messages", json={"body": "Still broken for me"})
    assert r.json()["data"]["status"] == "Open" and r.json()["data"]["resolved_at"] is None
    assert any(n.get("roles") == ["Admin", "CEO"] and n["event"] == "support.ticket_replied" for n in sent)


def _make_admin(db, uid, name):
    """Give login `uid` the Admin role and a name. Inspector + DDL run FIRST:
    on the shared StaticPool connection they roll back anything only flushed."""
    from models import Role, RoleName, UserRole
    have = {c["name"] for c in sa.inspect(db.get_bind()).get_columns("registration_data")}
    if "full_name" not in have:
        db.execute(sa.text("ALTER TABLE registration_data ADD COLUMN full_name TEXT"))
    if "username" not in have:
        db.execute(sa.text("ALTER TABLE registration_data ADD COLUMN username TEXT"))
    db.commit()
    role = db.query(Role).filter_by(name=RoleName.ADMIN).first()
    if role is None:
        role = Role(name=RoleName.ADMIN); db.add(role); db.flush()
    db.add(UserRole(user_id=uid, role_id=role.id))
    db.execute(sa.text("UPDATE registration_data SET full_name = :n, username = :u WHERE id = :i"),
               {"n": name, "u": name.lower().replace(" ", ""), "i": uid})
    db.commit()


def test_non_staff_cannot_patch_and_bad_transitions_are_rejected(world):
    client, as_, _, db = world
    tid = _raise(client).json()["data"]["id"]
    assert client.patch(f"/api/support/tickets/{tid}", json={"status": "Closed"}).status_code == 403
    as_(ADMIN)
    client.patch(f"/api/support/tickets/{tid}", json={"status": "Closed"})
    r = client.patch(f"/api/support/tickets/{tid}", json={"status": "Resolved"})   # Closed → Resolved not allowed
    assert r.status_code == 400
    assert client.patch(f"/api/support/tickets/{tid}", json={"priority": "Nope"}).status_code == 400
    # assignee must be an Admin/CEO login — resolved server-side, never client-named
    assert client.patch(f"/api/support/tickets/{tid}", json={"assigned_to": 2}).status_code == 400
    _make_admin(db, 1, "Karan Singh")
    r = client.patch(f"/api/support/tickets/{tid}", json={"assigned_to": 1})
    assert r.json()["data"]["assigned_to_name"] == "Karan Singh"


def test_rating_only_when_resolved_only_by_owner_and_only_once(world):
    client, as_, _, _ = world
    tid = _raise(client).json()["data"]["id"]
    assert client.post(f"/api/support/tickets/{tid}/rating", json={"rating": 4}).status_code == 409
    as_(ADMIN)
    client.patch(f"/api/support/tickets/{tid}", json={"status": "Resolved"})
    assert client.post(f"/api/support/tickets/{tid}/rating", json={"rating": 4}).status_code == 403
    as_(USER)
    assert client.post(f"/api/support/tickets/{tid}/rating", json={"rating": 4}).status_code == 200
    assert client.post(f"/api/support/tickets/{tid}/rating", json={"rating": 1}).status_code == 409


def test_other_users_cannot_reply_or_rate_and_closed_rejects_user_replies(world):
    client, as_, _, _ = world
    tid = _raise(client).json()["data"]["id"]
    as_(OTHER)
    assert client.post(f"/api/support/tickets/{tid}/messages", json={"body": "hijack"}).status_code == 404
    assert client.post(f"/api/support/tickets/{tid}/rating", json={"rating": 5}).status_code == 404
    as_(ADMIN)
    client.patch(f"/api/support/tickets/{tid}", json={"status": "Closed"})
    as_(USER)
    assert client.post(f"/api/support/tickets/{tid}/messages", json={"body": "still broken"}).status_code == 400


def test_admin_raising_own_ticket_is_treated_as_the_user(world):
    client, as_, sent, _ = world
    as_(ADMIN)
    tid = _raise(client, subject="Admin's own issue").json()["data"]["id"]
    sent.clear()
    r = client.post(f"/api/support/tickets/{tid}/messages", json={"body": "more details"})
    m = r.json()["data"]["messages"][-1]
    assert m["is_staff"] is False and r.json()["data"]["status"] == "Open"   # not auto In_Progress
    assert any(n.get("roles") == ["Admin", "CEO"] for n in sent)            # other admins told (self excluded upstream)


def test_reply_bumps_updated_at_so_queue_order_is_by_activity(world):
    client, as_, _, _ = world
    a = _raise(client, subject="first").json()["data"]["id"]
    b = _raise(client, subject="second").json()["data"]["id"]
    as_(ADMIN)
    client.post(f"/api/support/tickets/{b}/messages", json={"body": "ack"})
    client.post(f"/api/support/tickets/{a}/messages", json={"body": "ack"})
    order = [t["id"] for t in client.get("/api/support/tickets").json()["data"]]
    assert order[0] == a and order[1] == b
    assert len(client.get("/api/support/tickets?mine=1").json()["data"]) == 0   # staff's own = none


def test_list_filters_and_summary(world):
    client, as_, _, _ = world
    _raise(client); _raise(client, subject="Second", priority="Low", category="How_To")
    as_(ADMIN)
    assert len(client.get("/api/support/tickets?priority=Low").json()["data"]) == 1
    assert len(client.get("/api/support/tickets?status=open").json()["data"]) == 2
    assert len(client.get("/api/support/tickets?search=Second").json()["data"]) == 1
    assert client.get("/api/support/tickets/summary").json()["data"]["open"] == 2
    assert client.get("/api/support/tickets/meta").json()["data"]["is_staff"] is True


def test_bot_escalation_marker_and_offline_fallback(monkeypatch):
    from ai_help import assist as ai
    # offline: no provider → KB answer + escalate
    monkeypatch.setattr(ai, "_assist_llm_purpose", lambda: None)
    out = svc.bot_reply(message="how do I apply leave", route="my-leave", history=[], user_label="x")
    assert out["escalate"] is True and "ticket" in out["reply"].lower()

    # online: the marker is parsed and stripped
    class _Msg:  # noqa: D401
        content = "Open Settings ▸ Backup and click Generate.\nESCALATE: no"

    class _Res:
        choices = [type("C", (), {"message": _Msg()})()]
    monkeypatch.setattr(ai, "_assist_llm_purpose", lambda: "default")
    import services.support as ss
    monkeypatch.setattr("openai_client.get_openai_client", lambda p: object())
    monkeypatch.setattr("prompt_logger.tracked_chat_completion", lambda *a, **k: _Res())
    out = ss.bot_reply(message="where is backup", route="settings", history=[], user_label="x")
    assert out["escalate"] is False and "ESCALATE" not in out["reply"] and out["reply"].startswith("Open Settings")
    _Msg.content = "That looks like a bug — please raise a ticket.\nESCALATE: yes"
    out = ss.bot_reply(message="numbers wrong", route="invoices", history=[], user_label="x")
    assert out["escalate"] is True and "ESCALATE" not in out["reply"]
    # marker before a NAVIGATE_TO line is still found and stripped; a quoted marker mid-text is ignored
    _Msg.content = "Go to Timesheets.\nESCALATE: no\nNAVIGATE_TO: timesheets"
    out = ss.bot_reply(message="x", route="timesheets", history=[], user_label="x")
    assert out["escalate"] is False and "ESCALATE" not in out["reply"] and out["navigate_to"] == "timesheets"
    assert ss.split_escalation_marker("You wrote 'ESCALATE: yes' but this is fine.") == (
        "You wrote 'ESCALATE: yes' but this is fine.", False)


def test_chat_endpoint_never_500s(world, monkeypatch):
    client, *_ = world
    monkeypatch.setattr(svc, "bot_reply", lambda **k: (_ for _ in ()).throw(RuntimeError("boom")))
    r = client.post("/api/support/chat", json={"message": "help", "route": "profiles", "history": []})
    assert r.status_code == 200 and r.json()["data"]["escalate"] is True
