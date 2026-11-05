"""Karnex Careers — one public link for every open position (3 Nov 2026).

The page reads Orbit live (approved, open-for-sourcing positions, RMG JD, no
customer name); an application lands with TA at Sourcing, already interested —
no opening email, no Interested step.

Run:  cd backend && python -m pytest tests/test_careers.py -q
"""
from __future__ import annotations

import importlib
import io

import pytest
from fastapi import HTTPException, UploadFile
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
]:
    importlib.import_module(f"models.{_m}")

from models.base import Base  # noqa: E402
from models import (  # noqa: E402
    CandidateProfile, Customer, Location, Opportunity, OpportunityApprovalStatus, OppType,
    PipelineStage, PipelineStatus, Requirement, RequirementSkill, RequirementStatus, Resume, Skill,
)
from routers.crm import apply as ap  # noqa: E402
from routers.crm import public_careers as pc  # noqa: E402
from services import careers  # noqa: E402
from services.opening_interest import opening_states, send_opening_mail  # noqa: E402


@pytest.fixture()
def db():
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


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("AUTH_SECRET", "t" * 48)
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://karnexgroup.com")
    import services.invite_links as il
    monkeypatch.setattr(il, "configured_base", lambda: "https://karnexgroup.com")


_n = iter(range(1, 1000))


def _position(db, *, status=RequirementStatus.OPEN_FOR_SOURCING, stage=PipelineStage.ACTIVE,
              approval=OpportunityApprovalStatus.APPROVED, city="Pune", title="AUTOSAR Developer",
              customer="Harman", jd=None):
    i = next(_n)
    cust = db.execute(select(Customer).where(Customer.name == customer)).scalars().first()
    if cust is None:
        cust = Customer(name=customer, legal_entity_name=f"{customer} International Pvt Ltd")
        db.add(cust); db.flush()
    opp = Opportunity(opp_id=f"C-2026-{i:05d}", title=title, customer_id=cust.id,
                      opp_type=OppType.T_AND_M, created_by=1, pipeline_stage=stage,
                      approval_status=approval)
    db.add(opp); db.flush()
    loc = db.execute(select(Location).where(Location.city == city)).scalars().first()
    if loc is None:
        loc = Location(city=city, state="Maharashtra")
        db.add(loc); db.flush()
    req = Requirement(req_number=f"REQ-{i}", opportunity_id=opp.id, customer_id=cust.id, title=title,
                      no_of_positions=3, status=status, description="sales notes",
                      rmg_jd_text=jd if jd is not None else f"Build AUTOSAR stacks for {customer} ECUs.",
                      experience_min=4, experience_max=8, location_id=loc.id, created_by=1)
    db.add(req); db.flush()
    db.commit()
    return opp, req


def _upload(name="cv.pdf"):
    return UploadFile(file=io.BytesIO(b"%PDF-1 resume"), filename=name)


@pytest.fixture()
def sent(monkeypatch):
    out: list = []
    import services.notify as n
    monkeypatch.setattr(n, "notify_user", lambda _db, uid, *a, **k: out.append(("user", uid, k.get("event"))))
    monkeypatch.setattr(n, "notify_role", lambda _db, role, *a, **k: out.append(("role", role, k.get("event"))) or 1)
    monkeypatch.setattr(pc, "save_upload_hashed", lambda f, d: ("/u/cv.pdf", f"sha-{next(_n)}", 10))
    monkeypatch.setattr(ap, "save_upload_hashed", lambda f, d: ("/u/cv.pdf", f"sha-{next(_n)}", 10))
    return out


def _apply(db, req, **over):
    kw = dict(candidate_name="Asha Rao", email="asha@example.com", phone="+91 98765 43210",
              experience="5", notice_period="30 days", last_working_day="", current_location="Nagpur",
              preferred_location="", current_ctc="", expected_ctc="18 LPA", skills="", linkedin_url="",
              consent="true", src="linkedin", file=_upload(), db=db)
    kw.update(over)
    return pc.public_apply(object(), req.id, **kw)


def test_the_page_lists_only_open_approved_positions_with_the_rmg_jd(db):
    _, live = _position(db, title="AUTOSAR Developer", city="Pune")
    _position(db, title="Pending one", status=RequirementStatus.PENDING_ENGINEERING_REVIEW)
    _position(db, title="Held deal", stage=PipelineStage.ON_HOLD)
    _position(db, title="Unapproved deal", approval=OpportunityApprovalStatus.PENDING_SALES_HEAD_APPROVAL)
    _position(db, title="Filled", status=RequirementStatus.FULFILLED)
    _, b = _position(db, title="Android Developer", city="Bengaluru", customer="Visteon")
    data = careers.careers_listing(db)
    titles = [p["title"] for p in data["positions"]]
    assert titles == ["Android Developer", "AUTOSAR Developer"]
    assert data["total_openings"] == 6
    assert {c["city"] for c in data["locations"]} == {"Pune", "Bangalore"}
    item = careers.careers_position(db, live.id)
    assert item["experience"] == "4–8 yrs" and item["location"] == "Pune, Maharashtra"
    # RMG JD, not the Sales description — and the customer is never named.
    assert "AUTOSAR stacks" in item["jd"] and "sales notes" not in item["jd"]
    assert "Harman" not in item["jd"] and "our client" in item["jd"]
    assert "customer" not in " ".join(item.keys())


def test_a_change_in_orbit_reaches_the_page_at_once(db):
    _, req = _position(db)
    assert careers.careers_position(db, req.id) is not None
    req.status = RequirementStatus.ON_HOLD; db.commit()
    assert careers.careers_position(db, req.id) is None
    req.status = RequirementStatus.IN_PROGRESS; db.commit()
    assert careers.careers_position(db, req.id) is not None
    # RMG's switch takes it off without touching the status.
    careers.set_listed(db, req, False, None); db.commit()
    assert careers.careers_position(db, req.id) is None
    assert any(p["id"] == req.id and not p["listed"] for p in careers.staff_overview(db)["positions"])


def test_only_the_latest_requirement_of_a_deal_is_listed(db):
    opp, old = _position(db)
    newer = Requirement(req_number="REQ-NEW", opportunity_id=opp.id, customer_id=opp.customer_id,
                        title="AUTOSAR Developer (v2)", status=RequirementStatus.OPEN_FOR_SOURCING,
                        created_by=1)
    db.add(newer); db.commit()
    ids = [p["id"] for p in careers.careers_listing(db)["positions"]]
    assert ids == [newer.id]


def test_skills_are_listed_mandatory_first(db):
    _, req = _position(db)
    for name, must in (("CAN", False), ("AUTOSAR", True)):
        sk = Skill(name=name); db.add(sk); db.flush()
        db.add(RequirementSkill(requirement_id=req.id, skill_id=sk.id, is_mandatory=must))
    db.commit()
    assert careers.careers_position(db, req.id)["skills"] == [
        {"name": "AUTOSAR", "must": True}, {"name": "CAN", "must": False}]


def test_a_careers_applicant_lands_at_sourcing_already_interested(db, sent):
    opp, req = _position(db)
    out = _apply(db, req)
    assert out["data"]["received"] is True
    resume = db.execute(select(Resume).where(Resume.requirement_id == req.id)).scalars().one()
    assert resume.source_portal == careers.CAREERS_PORTAL
    assert resume.application_details["source_channel"] == "LinkedIn"
    prof = db.execute(select(CandidateProfile).where(CandidateProfile.opportunity_id == opp.id)).scalars().one()
    assert prof.pipeline_status == PipelineStatus.SOURCING and prof.source == "careers"
    state = opening_states(db, [prof.id])[prof.id]
    assert state["state"] == "interested" and state["via"] == "careers"
    assert "LinkedIn" in state["note"] and not state["note"].startswith("[")
    assert sent == [("role", "TA", "careers.application")]
    # No opening email for someone who applied themselves.
    from models import Candidate
    res = send_opening_mail(db, prof, db.get(Candidate, prof.candidate_id), req, None)
    assert res["status"] == "already_interested"


def test_where_applications_come_from_is_counted_per_channel(db, sent):
    """5 Nov 2026, user ask: see which platform brings candidates. LinkedIn is the
    `_apply` default; a plain link (no `?src=`) is the Karnex careers page."""
    opp, req = _position(db)
    _apply(db, req)
    _apply(db, req, src="", email="direct@example.com", file=_upload("cv2.pdf"))
    stats = {r["key"]: r for r in careers.channel_stats(db)}
    assert [k for k in stats][:5] == ["linkedin", "naukri", "whatsapp", "indeed", "karnex"]
    assert stats["linkedin"]["applications"] == 1 and stats["linkedin"]["last_30_days"] == 1
    assert stats["karnex"]["applications"] == 1 and stats["karnex"]["label"] == "Karnex careers page"
    assert stats["naukri"]["applications"] == 0 and stats["naukri"]["moved_on_pct"] is None
    # A candidate who moved past TA's Sourcing counts as "in process".
    prof = db.execute(select(CandidateProfile).where(CandidateProfile.opportunity_id == opp.id)
                      .order_by(CandidateProfile.id)).scalars().first()
    prof.pipeline_status = PipelineStatus.RMG_REVIEW; db.commit()
    stats = {r["key"]: r for r in careers.channel_stats(db)}
    assert stats["linkedin"]["in_process"] + stats["karnex"]["in_process"] == 1
    assert "by_channel" in careers.staff_overview(db)


def test_the_apply_link_is_treated_the_same(db, sent):
    opp, req = _position(db)
    ap.submit_application(
        object(), ap.make_apply_token(req.id), candidate_name="Ravi K", email="ravi@example.com",
        phone="9876543210", experience="4", education="", technical_domain="", skills="C",
        notice_period="30 days", current_ctc="", expected_ctc="", preferred_location="",
        current_location="Pune", file=_upload(), db=db)
    prof = db.execute(select(CandidateProfile).where(CandidateProfile.opportunity_id == opp.id)).scalars().one()
    assert opening_states(db, [prof.id])[prof.id]["via"] == "apply_link"
    assert sent == [("role", "TA", "apply_link.application")]


def test_apply_refuses_a_closed_position_and_missing_consent(db, sent):
    _, req = _position(db)
    with pytest.raises(HTTPException) as e:
        _apply(db, req, consent="")
    assert e.value.status_code == 400
    with pytest.raises(HTTPException) as e:
        _apply(db, req, email="not-an-email")
    assert e.value.status_code == 400
    careers.set_listed(db, req, False, None); db.commit()
    with pytest.raises(HTTPException) as e:
        _apply(db, req)
    assert e.value.status_code == 404
    with pytest.raises(HTTPException):
        pc.public_position(object(), req.id, db=db)


def test_a_candidate_already_past_sourcing_is_not_reset(db, sent):
    opp, req = _position(db)
    _apply(db, req)
    prof = db.execute(select(CandidateProfile).where(CandidateProfile.opportunity_id == opp.id)).scalars().one()
    prof.pipeline_status = PipelineStatus.RMG_REVIEW; db.commit()
    _apply(db, req, file=_upload("cv2.pdf"))
    db.refresh(prof)
    assert prof.pipeline_status == PipelineStatus.RMG_REVIEW


def test_post_text_and_pure_helpers():
    listed = [{"title": f"Role {i}", "city": "Pune", "experience": "3–6 yrs"} for i in range(5)]
    text = careers.post_text(listed, "https://karnexgroup.com/careers?src=linkedin")
    assert "• Role 0 — Pune, 3–6 yrs" in text and "…and 2 more" in text
    assert "https://karnexgroup.com/careers?src=linkedin" in text
    assert careers.channel_label("LinkedIn") == "LinkedIn"
    assert careers.channel_label("<script>") == "Karnex careers page"   # 5 Nov 2026 wording
    assert careers.is_direct("Careers page") and careers.is_direct(None) and not careers.is_direct("Naukri")
    assert careers.scrub_customer("Work for HARMAN and Harman's ECU team", ["Harman"]) == \
        "Work for our client and our client's ECU team"
    assert careers.scrub_customer("Aptiv", ["Ap"]) == "Aptiv"


def test_staff_overview_builds_the_link_and_the_post(db):
    _position(db)

    class _Req:
        headers: dict = {}
        base_url = "http://orbit.example.com/"

    class _U:
        is_admin = False

        def has_any(self, *n):
            return "RMG" in n

    data = pc.careers_overview(_Req(), db=db, user=_U())["data"]
    assert data["url"] == "https://karnexgroup.com/careers"
    assert data["can_edit"] is True and len(data["positions"]) == 1
    assert data["url"] in data["post_text"]


def test_a_local_public_address_gives_way_to_the_origin_in_use(db, monkeypatch):
    """Settings said http://localhost:2020 while the app serves HTTPS → the link
    opened to ERR_EMPTY_RESPONSE. A non-public address is replaced by the origin the
    dashboard was opened on, so the link opens on this server."""
    import services.invite_links as il
    monkeypatch.setattr(il, "configured_base", lambda: "http://localhost:2020")
    _position(db)

    class _Req:
        headers: dict = {}
        base_url = "https://localhost:2020/"

    class _U:
        is_admin = True

        def has_any(self, *n):
            return False

    data = pc.careers_overview(_Req(), db=db, user=_U())["data"]
    assert data["url"] == "https://localhost:2020/careers"
    assert data["base_reach"] == "private" and data["base_source"] == "request"


def test_a_general_resume_lands_in_candidates_marked_from_the_careers_page(db, sent):
    from models import Candidate
    pc.public_general_apply(object(), candidate_name="Ravi Kumar", email="ravi@example.com",
                            phone="+91 98765 43210", experience="4", current_location="Pune",
                            preferred_location="Bengaluru", skills="CAN, UDS", consent="on", src="linkedin", file=_upload(), db=db)
    cand = db.execute(select(Candidate).where(Candidate.email == "ravi@example.com")).scalars().one()
    assert cand.cv_url == "/u/cv.pdf"
    assert cand.created_by_name == "Careers page · LinkedIn"
    assert cand.preferred_locations
    assert ("role", "TA", "careers.general_application") in sent


def test_serving_notice_needs_a_sensible_last_working_day():
    from datetime import date
    today = date(2026, 11, 3)
    assert careers.lwd_problem("30 days", "", today) == (None, None)
    assert careers.lwd_problem("Serving notice", "", today)[1]
    assert careers.lwd_problem("Serving notice", "next week", today)[1]
    assert careers.lwd_problem("Serving notice", "2026-09-01", today)[1]
    assert careers.lwd_problem("Serving notice", "2027-12-01", today)[1]
    assert careers.lwd_problem("Serving notice", "2026-11-28", today) == (date(2026, 11, 28), None)


def test_an_application_fills_the_candidate_with_ctc_notice_and_last_day(db, sent):
    from datetime import date, timedelta
    from models import Candidate
    _, req = _position(db, title="AUTOSAR Developer", city="Pune")
    lwd = (date.today() + timedelta(days=20)).isoformat()
    with pytest.raises(HTTPException):
        _apply(db, req, notice_period="Serving notice")
    _apply(db, req, notice_period="Serving notice", last_working_day=lwd,
           current_ctc="12 LPA", preferred_location="Bengaluru")
    cand = db.execute(select(Candidate).where(Candidate.email == "asha@example.com")).scalars().one()
    assert cand.last_working_day.isoformat() == lwd and cand.resignation_status is True
    assert float(cand.current_ctc) == 1_200_000 and float(cand.expected_ctc) == 1_800_000
    assert cand.notice_period == "Serving notice"
    assert cand.preferred_locations


def test_careers_route_is_declared_before_the_frontend_mount():
    from pathlib import Path
    src = Path(__file__).resolve().parents[1].joinpath("main.py").read_text(encoding="utf-8")
    assert src.index('@app.get("/careers"') < src.index('app.mount("/", StaticFiles')


def test_the_assigned_ta_owns_a_self_applied_candidate(db, sent):
    from models import RequirementTaAssignment
    opp, req = _position(db)
    db.add(RequirementTaAssignment(requirement_id=req.id, user_id=1, user_name="Tara")); db.commit()
    _apply(db, req)
    prof = db.execute(select(CandidateProfile).where(CandidateProfile.opportunity_id == opp.id)).scalars().one()
    assert prof.ta_owner_id == 1 and prof.ta_owner_name == "Tara"
    assert sent == [("user", 1, "careers.application")]


def test_a_failing_cv_autofill_never_costs_the_sourcing_profile(db, sent, monkeypatch):
    """5 Nov 2026 — reported: a careers application never reached Sourcing. The
    form's skills / CTC autofill ran outside a savepoint; when it failed, the
    candidate and the profile were lost with it."""
    import services.candidates as sc

    def boom(*_a, **_k):
        raise RuntimeError("skill master clash")
    monkeypatch.setattr(sc, "apply_cv_profile_to_candidate", boom)
    import services.slot_booking as sb
    monkeypatch.setattr(sb, "apply_cv_profile_to_candidate", boom, raising=False)
    opp, req = _position(db)
    out = _apply(db, req, skills="AUTOSAR, CAN")
    assert out["data"]["received"] is True
    prof = db.execute(select(CandidateProfile).where(CandidateProfile.opportunity_id == opp.id)).scalars().one()
    assert prof.pipeline_status == PipelineStatus.SOURCING


def test_the_same_cv_again_still_gets_its_sourcing_profile(db, sent, monkeypatch):
    """A first attempt that saved the CV but no profile is repaired by the next one."""
    monkeypatch.setattr(pc, "save_upload_hashed", lambda f, d: ("/u/cv.pdf", "sha-same", 10))
    opp, req = _position(db)
    db.add(Resume(requirement_id=req.id, candidate_name="Asha Rao", email="asha@example.com",
                  source_portal=careers.CAREERS_PORTAL, resume_file_url="/u/cv.pdf",
                  file_sha256="sha-same", file_size=10))
    db.commit()
    out = _apply(db, req)
    assert out["data"]["received"] is True
    prof = db.execute(select(CandidateProfile).where(CandidateProfile.opportunity_id == opp.id)).scalars().one()
    assert prof.pipeline_status == PipelineStatus.SOURCING and prof.source == "careers"


def test_applied_by_me_keeps_the_self_applied_candidates():
    """TA's default "Applied by = me" hid every careers applicant (nobody applied
    them); `with_public` keeps them."""
    from routers.crm import resumes as rr
    mine = str(rr._applied_by_clause("Tara").compile(compile_kwargs={"literal_binds": True}))
    both = str(rr._applied_by_clause("Tara", True).compile(compile_kwargs={"literal_binds": True}))
    assert "source" not in mine
    assert "careers" in both and "apply_link" in both and "Tara" in both


def test_the_ta_notice_links_straight_to_the_applicant(db, monkeypatch):
    seen: list = []
    import services.notify as n
    monkeypatch.setattr(n, "notify_role", lambda _db, role, title, msg, link, **k: seen.append(link) or 1)
    _, req = _position(db)
    resume = Resume(requirement_id=req.id, candidate_name="Asha Rao", email="asha+x@example.com",
                    resume_file_url="/u/cv.pdf", file_sha256="sha-link", file_size=10)
    db.add(resume); db.commit()
    careers.notify_tas(db, req, resume, "Asha Rao", via="careers")
    assert seen and seen[0].endswith("&tab=resumes&q=asha%2Bx%40example.com")


def test_stored_location_text_becomes_clean_unique_cities():
    from services.careers import split_cities
    assert split_cities("['Bangalore']") == ["Bangalore"]
    assert split_cities("['Bangalore', 'Chennai']") == ["Bangalore", "Chennai"]
    assert split_cities("['Chennai', 'Bangalore']") == ["Chennai", "Bangalore"]
    assert split_cities(["Bengaluru", "bangalore", "Pune, Maharashtra"]) == ["Bangalore", "Pune"]
    assert split_cities("Chennai / Hyderabad and Remote") == ["Chennai", "Hyderabad"]
    assert split_cities(None) == [] and split_cities("[]") == []
