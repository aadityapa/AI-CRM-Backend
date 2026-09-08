"""Settings → Email Drafts shows the CURRENT built-in wording (3 Sep 2026).

User request: "give me current draft — if I want I can change here only".
The editor prefills each candidate email with what goes out today, built by
running the real message builders with `{token}` placeholders as the values,
so the shown text can never drift from the sent text.

Run:  cd backend && python -m pytest tests/test_email_drafts_builtin.py -q
"""
from __future__ import annotations

import importlib

for _m in ["base", "rbac", "candidates", "email_outbox"]:
    importlib.import_module(f"models.{_m}")

from routers.crm.email_flows import EVENTS  # noqa: E402
from services.candidate_comms import (  # noqa: E402
    builtin_candidate_draft, customer_round_invite_message, internal_round_invite_message,
)

CANDIDATE_EVENTS = [e for e in EVENTS if e.get("kind") == "candidate"]


def test_every_candidate_email_has_a_built_in_draft_to_show():
    assert {e["event"] for e in CANDIDATE_EVENTS} >= {
        "candidate.slot_invite", "candidate.ai_invite", "candidate.l1_manual_invite",
        "candidate.l2_invite", "candidate.hr_invite", "candidate.round_invite",
        "candidate.hiring_interest",
    }
    for spec in CANDIDATE_EVENTS:
        d = builtin_candidate_draft(spec["event"])
        assert d and d["subject"].strip() and d["body"].strip(), spec["event"]


def test_drafts_carry_their_placeholders_literally():
    """The editor shows {candidate}, {link}… — not a sample name — so an admin
    keeps the substitution points when rewording."""
    d = builtin_candidate_draft("candidate.slot_invite")
    assert "{candidate}" in d["body"] and "{link}" in d["body"] and "{role}" in d["subject"]
    d = builtin_candidate_draft("candidate.ai_invite")
    for tok in ("{candidate}", "{role}", "{link}", "{access_key}", "{sender}", "{company}"):
        assert tok in d["subject"] + d["body"], tok
    d = builtin_candidate_draft("candidate.l1_manual_invite")
    assert "(L1)" in d["body"] and "{interviewer}" in d["body"]
    d = builtin_candidate_draft("candidate.hr_invite")
    assert "HR team" in d["body"]
    d = builtin_candidate_draft("candidate.round_invite")
    assert "{round}" in d["body"] and "{round}" in d["subject"]
    d = builtin_candidate_draft("candidate.hiring_interest")
    assert "{{first_name}}" in d["body"]
    assert builtin_candidate_draft("candidate.direct_message") is None


def test_round_builders_keep_the_original_wording():
    s, b = internal_round_invite_message("Asha", "L2", "engineering", "Ravi", "Mon 10:00", "https://x", None)
    assert s == "Interview invitation — L2 round"
    assert "Your interview round (L2) has been scheduled with our engineering team." in b
    assert "Interviewer: Ravi\n" in b and "When: Mon 10:00\n" in b and "Meeting link: https://x\n" in b
    s, b = customer_round_invite_message("Asha", "Customer L1 Interview", "Mon", "https://x", "bring ID")
    assert s == "Interview invitation — Customer L1 Interview"
    assert "\nbring ID\n" in b and "Please join a few minutes early" in b


def test_every_advertised_token_is_used_by_the_default_or_send_path():
    """A chip the editor offers must mean something at send time."""
    expected_used = {
        "candidate.l2_invite": {"candidate", "round", "team", "interviewer", "when", "link", "note"},
        "candidate.round_invite": {"candidate", "round", "when", "link", "note"},
    }
    for event, toks in expected_used.items():
        spec = next(e for e in EVENTS if e["event"] == event)
        assert toks <= set(spec["tokens"])


# ------------------------------------------- internal layout + custom drafts

import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.dialects.postgresql import ARRAY, INET, JSONB, UUID  # noqa: E402
from sqlalchemy.ext.compiler import compiles  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402


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


@pytest.fixture()
def db():
    for _m in ["notify_routes", "user_profiles", "access_templates"]:
        importlib.import_module(f"models.{_m}")
    from models.base import Base
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    s = Session(bind=engine, future=True)
    from models.base import users_table_stub
    s.execute(users_table_stub.insert().values(id=1))
    s.commit()
    try:
        yield s
    finally:
        s.close()


def test_internal_layout_places_the_parts(db):
    """The layout the Email Drafts tab shows for internal notifications must
    render, token for token, through the same path the outbox uses."""
    from models import NotificationRoute
    from services.email_outbox import INTERNAL_DEFAULT_BODY, _apply_event_template
    db.add(NotificationRoute(event="timesheet.submitted", roles=[], extra_emails=[],
                             enabled=True, subject_template="[{company}] {subject}",
                             body_template=INTERNAL_DEFAULT_BODY))
    db.commit()
    subj, body = _apply_event_template(
        db, "timesheet.submitted", subject="Timesheet submitted", body_text="whole text",
        to_name="Karan",
        context={"title": "Timesheet submitted", "message": "Asha submitted August.",
                 "details": "Project: Visteon", "link": "https://karnex.in/admin?p=timesheets/1"})
    assert subj.endswith("Timesheet submitted")
    assert body == ("Timesheet submitted\n\nAsha submitted August.\n\nProject: Visteon\n\n"
                    "Open in Karnex: https://karnex.in/admin?p=timesheets/1\n\n— Karnex")
    # No details and no link: the blank runs collapse instead of leaving holes.
    _, body = _apply_event_template(
        db, "timesheet.submitted", subject="S", body_text="x", to_name="",
        context={"title": "S", "message": "M", "details": "", "link": ""})
    assert body == "S\n\nM\n\n— Karnex"


def _admin_client(db):
    import crm_deps
    import routers.crm.email_flows as flows_router
    app = FastAPI()
    app.include_router(flows_router.router)
    app.dependency_overrides[crm_deps.get_crm_db] = lambda: db
    app.dependency_overrides[crm_deps.get_current_user] = lambda: crm_deps.CurrentUser(
        id=1, username="admin", roles={"Admin"})
    return TestClient(app)


def test_custom_drafts_round_trip(db):
    c = _admin_client(db)
    r = c.post("/api/email-flows/custom", json={
        "label": "Document request", "description": "Before onboarding",
        "subject": "Documents for {role}", "body": "Hello {candidate},\nplease share…\n{sender}"})
    assert r.status_code == 200, r.text
    key = r.json()["data"]["event"]
    assert key == "custom.document-request"
    # Same label again → suffixed, never a 409.
    r2 = c.post("/api/email-flows/custom", json={
        "label": "Document request", "subject": "s", "body": "b"})
    assert r2.json()["data"]["event"] == "custom.document-request-2"

    flows = c.get("/api/email-flows").json()["data"]["flows"]
    mine = [f for f in flows if f["kind"] == "custom"]
    assert [f["event"] for f in mine] == [key, "custom.document-request-2"]
    assert mine[0]["label"] == "Document request" and mine[0]["subject_template"] == "Documents for {role}"
    # Internal flows carry the layout + example slot; candidate flows their built-in.
    internal = next(f for f in flows if f["event"] == "timesheet.submitted")
    assert internal["default_body"].startswith("{title}") and "last_sent" in internal

    # Anyone with a CRM role can list them for the composer.
    r = c.get("/api/email-drafts/custom")
    assert [d["key"] for d in r.json()["data"]] == [key, "custom.document-request-2"]

    # Rename + reword through the ordinary save.
    r = c.put(f"/api/email-flows/{key}", json={
        "roles": [], "extra_emails": [], "enabled": True, "label": "Docs needed",
        "subject_template": "Docs for {role}", "body_template": "Hi {first_name}"})
    assert r.status_code == 200, r.text
    assert r.json()["data"]["label"] == "Docs needed"
    # A custom draft cannot be blanked — that is what Delete is for.
    r = c.put(f"/api/email-flows/{key}", json={
        "roles": [], "extra_emails": [], "enabled": True, "subject_template": "", "body_template": ""})
    assert r.status_code == 400

    r = c.delete(f"/api/email-flows/{key}")
    assert r.json()["message"] == "Draft deleted"
    assert [d["key"] for d in c.get("/api/email-drafts/custom").json()["data"]] == ["custom.document-request-2"]
