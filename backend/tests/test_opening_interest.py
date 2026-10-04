"""Bulk upload → "we have an opening, are you interested?" email (1 Oct 2026).

User ask: every candidate a TA bulk-uploads gets a professional mail about the
opening; the candidate replies; TA records Interested (→ Technical Screening)
or Not interested (→ Self Withdrawn). "Make sure the email from the resume is
proper, not any mistake" — so the extraction rules are pinned here too.

Run:  cd backend && python -m pytest tests/test_opening_interest.py -q
"""
from __future__ import annotations

import importlib

import pytest
from sqlalchemy import create_engine, select
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
    "email_outbox", "notify_routes",
]:
    importlib.import_module(f"models.{_m}")

from crm_deps import CurrentUser  # noqa: E402
from models.base import Base  # noqa: E402
from models import (  # noqa: E402
    Candidate, CandidateProfile, Customer, Opportunity, OppType, PipelineStatus as PS,
    Requirement, RequirementSkill, Skill,
)
import services.candidate_profiles as cp  # noqa: E402
from services import opening_interest as oi  # noqa: E402
from services.resume_parse import (  # noqa: E402
    _regex_parse, clean_email, find_resume_emails, pick_resume_email, reconcile_email,
)

TA = CurrentUser(id=1, username="ta", full_name="Gargee Joshi", email="gargee@karnex.in", roles={"TA"})


# --- the address read from the CV -------------------------------------------

@pytest.mark.parametrize("raw, want", [
    ("Siva8848@Gmail.com", "siva8848@gmail.com"),
    ("mailto:siva@gmail.com", "siva@gmail.com"),
    ("<siva@gmail.com>.", "siva@gmail.com"),
    ("siva@gmail.con", "siva@gmail.com"),
    ("siva@gmial.com", "siva@gmail.com"),
    ("9876543210siva.k@gmail.com", "siva.k@gmail.com"),      # phone glued by the PDF
    ("image001.png@01d9a2b3.4c5d6e70", ""),                  # Outlook artefact
    ("john@example.com", ""),                                # template address
    ("resume-12@noemail.karnex.local", ""),                  # our placeholder
    ("priya.9f3c@import.karnex.in", ""),
    ("siva..k@gmail.com", ""),
    (".siva@gmail.com", ""),
    ("siva@gmail", ""),
    ("", ""),
])
def test_clean_email(raw, want):
    assert clean_email(raw) == want


def test_split_and_obfuscated_addresses_are_rejoined():
    assert find_resume_emails("Email: siva.k @ gmail . com") == ["siva.k@gmail.com"]
    # A sentence ending before an address is not glued onto it.
    assert find_resume_emails("Bengaluru. ravi@gmail.com") == ["ravi@gmail.com"]
    assert find_resume_emails("siva[at]gmail[dot]com") == ["siva@gmail.com"]


def test_the_candidates_own_address_wins_over_hr_and_referees():
    text = ("Ravi Kumar\nhr@previousco.com\nravi.kumar91@gmail.com\n"
            "Reference: anil.mehta@infosys.com")
    assert pick_resume_email(text, "Ravi Kumar") == "ravi.kumar91@gmail.com"
    # No name to go by: a role mailbox still loses to a personal one.
    assert pick_resume_email("careers@acme.com\nx.y@gmail.com") == "x.y@gmail.com"
    assert _regex_parse(text)["email"] == "ravi.kumar91@gmail.com"


def test_the_model_may_choose_but_never_invent():
    text = "Ravi Kumar ravi.k@gmail.com  hr@acme.com"
    assert reconcile_email("hr@acme.com", text, "Ravi Kumar") == "hr@acme.com"     # literal
    assert reconcile_email("ravi@yahoo.com", text, "Ravi Kumar") == "ravi.k@gmail.com"  # invented
    assert reconcile_email("ravi.kumar@gmail.com", "ravi.kumar@gmail.\ncom", "") == "ravi.kumar@gmail.com"
    assert reconcile_email("made.up@gmail.com", "no address here", "") == ""


# --- the mail --------------------------------------------------------------------

@pytest.fixture()
def db(monkeypatch):
    monkeypatch.setattr(cp, "_notify_stage_owner", lambda *a, **k: None)
    monkeypatch.setattr(cp, "notify_rmg_new_applicant", lambda *a, **k: None)
    monkeypatch.setattr(cp, "rmg_gate_enabled", lambda: True)
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


def _setup(db, email="ravi.k@gmail.com"):
    cust = Customer(name="VISTEON")
    db.add(cust); db.flush()
    opp = Opportunity(opp_id="C-2026-00001", title="AUTOSAR Engineer", customer_id=cust.id,
                      opp_type=OppType.T_AND_M, created_by=1)
    db.add(opp); db.flush()
    req = Requirement(req_number="REQ-1", opportunity_id=opp.id, customer_id=cust.id,
                      title="AUTOSAR Engineer", experience_min=4, experience_max=8, created_by=1)
    sk1, sk2 = Skill(name="AUTOSAR"), Skill(name="Embedded C")
    cand = Candidate(first_name="Ravi", last_name="Kumar", email=email)
    db.add_all([req, sk1, sk2, cand]); db.flush()
    db.add_all([RequirementSkill(requirement_id=req.id, skill_id=sk2.id, is_mandatory=False),
                RequirementSkill(requirement_id=req.id, skill_id=sk1.id, is_mandatory=True)])
    p = CandidateProfile(candidate_id=cand.id, opportunity_id=opp.id, pipeline_status=PS.SOURCING)
    db.add(p); db.flush()
    return req, cand, p


def _outbox(db):
    from models import EmailOutbox
    return db.execute(select(EmailOutbox)).scalars().all()


def test_the_message_is_professional_and_never_names_the_client(db):
    req, _, _ = _setup(db)
    facts = oi.opening_facts(db, req)
    assert facts["experience"] == "4–8 years"
    assert facts["skills"] == "AUTOSAR, Embedded C"      # mandatory first
    msg = oi.opening_message("Ravi Kumar", facts, {"name": "Gargee Joshi", "company": "Karnex",
                                                     "company_name": "Karnex Software"}, db=None)
    assert msg["subject"] == "Job opportunity: AUTOSAR Engineer — are you interested?"
    body = msg["text"]
    assert body.startswith("Dear Ravi,")
    for phrase in ("Position:", "4–8 years", "reply to this email", "notice period",
                   "expected CTC", "Gargee Joshi"):
        assert phrase in body, phrase
    assert "VISTEON" not in body and "VISTEON" not in msg["html"]


def test_one_mail_per_candidacy_with_the_ta_as_reply_to(db):
    req, cand, p = _setup(db)
    res = oi.send_opening_mail(db, p, cand, req, TA)
    assert res == {"status": "sent", "email": "ravi.k@gmail.com"}
    rows = _outbox(db)
    assert len(rows) == 1 and rows[0].event == oi.EVENT
    assert rows[0].reply_to_email == "gargee@karnex.in"
    assert oi.send_opening_mail(db, p, cand, req, TA)["status"] == "already_sent"
    assert oi.send_opening_mail(db, p, cand, req, TA, resend=True)["status"] == "sent"
    assert len(_outbox(db)) == 2
    assert oi.opening_states(db, [p.id])[p.id]["state"] == "sent"


def test_a_placeholder_address_is_never_mailed(db):
    req, cand, p = _setup(db, email="resume-7@noemail.karnex.local")
    assert oi.send_opening_mail(db, p, cand, req, TA)["status"] == "no_email"
    # The address read from THIS CV wins over the record's.
    assert oi.send_opening_mail(db, p, cand, req, TA, to_email="Ravi.K@Gmail.con")["email"] \
        == "ravi.k@gmail.com"
    assert not oi.mailable_email("hr@example.com")


def test_interested_sends_for_screening_and_not_interested_withdraws(db):
    req, cand, p = _setup(db)
    oi.send_opening_mail(db, p, cand, req, TA)
    cp.ta_decision(db, p, "interested", "Confirmed CTC and notice on call", TA)
    assert p.rmg_screening_status == "Pending"
    assert oi.opening_states(db, [p.id])[p.id]["state"] == "interested"

    req2, cand2, p2 = _setup_second(db, req)
    cp.ta_decision(db, p2, "not_interested", None, TA)
    assert p2.pipeline_status == PS.SELF_WITHDRAWN
    assert oi.opening_states(db, [p2.id])[p2.id]["state"] == "not_interested"


def _setup_second(db, req):
    cand = Candidate(first_name="Asha", email="asha@gmail.com")
    db.add(cand); db.flush()
    p = CandidateProfile(candidate_id=cand.id, opportunity_id=req.opportunity_id,
                         pipeline_status=PS.SOURCING)
    db.add(p); db.flush()
    return req, cand, p


def test_the_draft_editor_shows_the_built_in_wording():
    from routers.crm.email_flows import EVENTS
    from services.candidate_comms import builtin_candidate_draft
    assert any(e["event"] == oi.EVENT and e["kind"] == "candidate" for e in EVENTS)
    d = builtin_candidate_draft(oi.EVENT)
    assert "{role}" in d["subject"] and "{first_name}" in d["body"] and "{sender}" in d["body"]
