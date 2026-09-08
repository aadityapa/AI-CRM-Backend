"""Emails tab — one row per conversation, server-side (3 Sep 2026).

User report: "all candidate emails mixed with each other". The old list
paged over MAILS and grouped in the browser, so a candidate straddled pages
and the order was whichever mail landed on the page. `email-threads` groups
on the server and pages over conversations, newest activity first.

Run:  cd backend && python -m pytest tests/test_email_threads.py -q
"""
from __future__ import annotations

import importlib
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
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
    "email_outbox",
]:
    importlib.import_module(f"models.{_m}")

from models.base import Base  # noqa: E402
import crm_deps  # noqa: E402
import routers.crm.candidates as candidates_router  # noqa: E402

T0 = datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)


def _mail(db, *, to, name, event, subject, at, status="Sent", cand_id=None):
    from models import EmailOutbox, EmailStatus
    row = EmailOutbox(
        event=event, to_email=to, to_name=name, subject=subject, body_text=f"body of {subject}",
        status=EmailStatus(status), attempts=0, next_attempt_at=at,
        related_type="candidate" if cand_id else None, related_id=cand_id,
    )
    db.add(row)
    db.flush()
    row.created_at = at
    db.flush()
    return row


@pytest.fixture()
def client():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = Session(bind=engine, future=True)
    from models.base import users_table_stub
    db.execute(users_table_stub.insert().values(id=1))
    db.commit()

    from models import Candidate
    a = Candidate(first_name="Asha", last_name="Rao", email="asha@example.com")
    b = Candidate(first_name="Bala", last_name="Kumar", email="bala@example.com")
    db.add_all([a, b])
    db.commit()

    # Asha: 3 mails, the newest of everything; Bala: 2 mails, one Failed;
    # interleaved in time so a naive per-mail list would mix them.
    _mail(db, to=a.email, name="Asha Rao", event="candidate.interview_link",
          subject="Asha #1", at=T0, cand_id=a.id)
    _mail(db, to=b.email, name="Bala Kumar", event="candidate.slot_invite",
          subject="Bala #1", at=T0 + timedelta(hours=1), cand_id=b.id)
    _mail(db, to=a.email, name="Asha Rao", event="candidate.direct_message",
          subject="Asha #2", at=T0 + timedelta(hours=2), cand_id=a.id)
    _mail(db, to=b.email, name="Bala Kumar", event="candidate.direct_message",
          subject="Bala #2", at=T0 + timedelta(hours=3), status="Failed", cand_id=b.id)
    _mail(db, to=a.email, name="Asha Rao", event="candidate.slot_invite",
          subject="Asha #3", at=T0 + timedelta(hours=4), cand_id=a.id)
    # An unstamped legacy row that matches Asha by address — same thread.
    _mail(db, to="ASHA@example.com", name=None, event="candidate.interview_link",
          subject="Asha legacy", at=T0 - timedelta(days=1))
    # Internal workflow mail — never on this tab.
    _mail(db, to="rmg@karnex.in", name="RMG", event="profile.rmg_screening_requested",
          subject="Internal", at=T0 + timedelta(hours=9))
    # Staff notification ABOUT Asha with the candidate.* prefix (3 Sep 2026):
    # stamped to her but addressed to TA — must NOT appear in her thread.
    _mail(db, to="ta@karnex.in", name="TA", event="candidate.joined",
          subject="Asha joined", at=T0 + timedelta(hours=10), cand_id=a.id)
    db.commit()

    app = FastAPI()
    app.include_router(candidates_router.router)
    app.dependency_overrides[crm_deps.get_crm_db] = lambda: db
    app.dependency_overrides[crm_deps.get_current_user] = lambda: crm_deps.CurrentUser(
        id=1, username="ta", roles={"TA"})
    return TestClient(app), a.id, b.id


def _threads(client, **params):
    r = client.get("/api/candidates/email-threads", params=params)
    assert r.status_code == 200, r.text
    body = r.json()
    return body["data"], body["meta"]


def test_one_row_per_candidate_newest_first(client):
    c, a_id, b_id = client
    data, meta = _threads(c)
    assert [d["candidate_id"] for d in data] == [a_id, b_id]
    assert meta["total"] == 2
    asha, bala = data
    assert asha["count"] == 4                       # 3 stamped + 1 legacy; "Asha joined" (to TA) excluded
    assert asha["latest"]["subject"] == "Asha #3"
    assert asha["latest"]["event_label"] == "Shortlisted — pick a slot"
    assert asha["key"] == f"cand:{a_id}"
    assert bala["count"] == 2 and bala["failed"] == 1
    assert bala["latest"]["subject"] == "Bala #2"
    assert bala["candidate_name"] == "Bala Kumar"


def test_paging_is_per_conversation(client):
    c, a_id, b_id = client
    p1, meta = _threads(c, limit=1, page=1)
    p2, _ = _threads(c, limit=1, page=2)
    assert meta["pages"] == 2
    assert [d["candidate_id"] for d in p1] == [a_id]
    assert [d["candidate_id"] for d in p2] == [b_id]


def test_filters_narrow_but_never_split_a_thread(client):
    c, a_id, b_id = client
    data, _ = _threads(c, status="Failed")
    assert [d["candidate_id"] for d in data] == [b_id]
    assert data[0]["count"] == 1                    # only the failed mail matched
    data, _ = _threads(c, search="asha")
    assert [d["candidate_id"] for d in data] == [a_id]
    data, _ = _threads(c, event="candidate.slot_invite")
    assert {d["candidate_id"] for d in data} == {a_id, b_id}


def test_sort_by_name(client):
    c, a_id, b_id = client
    data, _ = _threads(c, sort="name")
    assert [d["candidate_id"] for d in data] == [a_id, b_id]


def test_thread_endpoint_matches_the_list_key(client):
    """The reading pane fetches by candidate_id and keeps only rows whose key
    equals the list row's key — the legacy address-matched mail must land in
    the same bucket on both sides."""
    c, a_id, _ = client
    r = c.get("/api/candidates/email-conversations", params={"candidate_id": a_id, "limit": 100})
    assert r.status_code == 200, r.text
    rows = r.json()["data"]
    assert len(rows) == 4
    assert {x["candidate_id"] for x in rows} == {a_id}
