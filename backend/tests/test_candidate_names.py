"""12 Nov 2026 — one candidate name everywhere.

Reported: TA uploads a candidate (single or bulk ZIP), fixes the name with
Edit applicant, and the Candidates master keeps the old name.

Pinned: a name changed on Edit applicant or on the Candidates page reaches the
master AND every position's resume row (and is logged); an unchanged name in a
re-sent form changes nothing; an upload for a known candidate keeps the
master's name; parsed names are tidied.

Run:  cd backend && python -m pytest tests/test_candidate_names.py -q
"""
from __future__ import annotations

from types import SimpleNamespace

from sqlalchemy import select

from services import candidate_names as cn
from tests.test_upload_form_location_note import db, sb  # noqa: F401 — fixture + module
from models import (  # noqa: E402
    Candidate, CandidateProfile, CandidateProfileActivityLog, Customer, Opportunity, OppType,
    PipelineStatus as PS, Requirement, RequirementStatus, Resume,
)


def test_tidy_name_cleans_parser_and_file_name_noise():
    assert cn.tidy_name("RAVI_KUMAR_Resume") == "Ravi Kumar"
    assert cn.tidy_name("  ravi   kumar  ") == "Ravi Kumar"
    assert cn.tidy_name("Ravi Kumar CV 2026") == "Ravi Kumar"
    assert cn.tidy_name("John McKenzie") == "John McKenzie", "mixed case is kept as typed"
    assert cn.tidy_name("anne-marie d'souza") == "Anne-Marie D'Souza"
    assert cn.tidy_name("Resume") == "Resume", "never blanks a name made only of noise"
    assert cn.tidy_name("") == ""


def _two_positions(db):
    cust = Customer(name="C1"); db.add(cust); db.flush()
    opps = [Opportunity(opp_id=f"OPP-{i}", title="Embedded", customer_id=cust.id,
                        opp_type=OppType.T_AND_M, created_by=1) for i in (1, 2)]
    cand = Candidate(first_name="RAVI", last_name="KUMAR K", email="ravi@mail.com")
    db.add_all([*opps, cand]); db.flush()
    reqs = [Requirement(req_number=f"REQ-{o.id}", opportunity_id=o.id, customer_id=cust.id, title="Embedded",
                        no_of_positions=1, status=RequirementStatus.IN_PROGRESS, created_by=1) for o in opps]
    db.add_all(reqs); db.flush()
    rows = [Resume(candidate_name="RAVI KUMAR K", email="ravi@mail.com", candidate_id=cand.id,
                   requirement_id=r.id, resume_file_url="/api/crm-files/cv.pdf") for r in reqs]
    profs = [CandidateProfile(candidate_id=cand.id, opportunity_id=o.id, pipeline_status=PS.SOURCING)
             for o in opps]
    db.add_all([*rows, *profs]); db.flush()
    return cand, rows, profs


def test_rename_reaches_the_master_and_every_position(db):
    cand, rows, profs = _two_positions(db)
    out = cn.rename_candidate(db, cand, "Ravi Kumar", user_id=1, where="Edit applicant")
    assert out["changed"] and out["resumes"] == 2
    assert (cand.first_name, cand.middle_name, cand.last_name) == ("Ravi", None, "Kumar")
    assert {r.candidate_name for r in rows} == {"Ravi Kumar"}
    logs = db.execute(select(CandidateProfileActivityLog).where(
        CandidateProfileActivityLog.action_type == "NAME_CHANGED")).scalars().all()
    assert len(logs) == 2 and "RAVI KUMAR K" in logs[0].comment
    # The same name again changes nothing; a case-only fix is a change.
    assert cn.rename_candidate(db, cand, "Ravi Kumar")["changed"] is False


def test_candidates_page_keeps_the_typed_split(db):
    cand, rows, _ = _two_positions(db)
    out = cn.rename_candidate(db, cand, None, parts=("Ravi", "Prasad", "Kumar"))
    assert out["changed"] and (cand.first_name, cand.middle_name, cand.last_name) == ("Ravi", "Prasad", "Kumar")
    assert {r.candidate_name for r in rows} == {"Ravi Prasad Kumar"}


def test_edit_applicant_writes_the_master_only_when_ta_changed_the_value(db):
    from routers.crm.resumes import _update_candidate_master
    cand, rows, _ = _two_positions(db)
    user = SimpleNamespace(id=1)
    before = {"name": "RAVI KUMAR K", "email": "ravi@mail.com", "phone": None}
    # Dialog re-sends every field unchanged → nothing moves.
    assert _update_candidate_master(db, rows[0], {"candidate_name": "RAVI KUMAR K",
                                                  "email": "ravi@mail.com"}, before, user) == ""
    assert cand.first_name == "RAVI"
    note = _update_candidate_master(db, rows[0], {"candidate_name": "Ravi Kumar", "phone": "+91 98765 43210",
                                                  "email": "ravi.k@mail.com"}, before, user)
    assert "name updated in Candidates and 1 other position" in note
    assert (cand.first_name, cand.last_name, cand.phone, cand.email) == \
        ("Ravi", "Kumar", "+91 98765 43210", "ravi.k@mail.com")
    assert rows[1].candidate_name == "Ravi Kumar"


def test_edit_applicant_never_takes_another_candidates_email(db):
    from routers.crm.resumes import _update_candidate_master
    cand, rows, _ = _two_positions(db)
    db.add(Candidate(first_name="Other", email="other@mail.com")); db.flush()
    note = _update_candidate_master(db, rows[0], {"email": "other@mail.com"},
                                    {"name": "x", "email": "ravi@mail.com", "phone": None}, SimpleNamespace(id=1))
    assert "belongs to another candidate" in note and cand.email == "ravi@mail.com"


def test_an_upload_for_a_known_candidate_keeps_the_masters_name(db):
    cand = Candidate(first_name="Ravi", last_name="Kumar", email="ravi@mail.com")
    db.add(cand); db.flush()
    upload = Resume(candidate_name="RAVI K RESUME", email="ravi@mail.com")
    assert sb.find_or_create_candidate_from_resume(db, upload).id == cand.id
    assert (cand.first_name, cand.last_name) == ("Ravi", "Kumar")
    assert upload.candidate_name == "Ravi Kumar", "the row shows the master's name"


def test_a_new_upload_is_tidied_and_a_placeholder_master_takes_the_name(db):
    fresh = Resume(candidate_name="ASHA_RAO_CV", email="asha@mail.com")
    cand = sb.find_or_create_candidate_from_resume(db, fresh)
    assert (cand.first_name, cand.last_name, fresh.candidate_name) == ("Asha", "Rao", "Asha Rao")
    ph = Candidate(first_name="Unknown", email="kiran@mail.com"); db.add(ph); db.flush()
    up = Resume(candidate_name="Kiran Patil", email="kiran@mail.com")
    sb.find_or_create_candidate_from_resume(db, up)
    assert (ph.first_name, ph.last_name) == ("Kiran", "Patil")
