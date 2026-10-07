"""The same mail twice (29 Sep 2026).

Reported with a screenshot: one person received "AI interview completed: X"
twice and "AI L1 passed — review X" twice for ONE interview. Two causes:

* the interview report is saved more than once (quick fallback, then the AI
  upgrade with a slightly different score) and `sync_completed_interview`
  re-notified on every save whose score differed — it now announces only the
  first verdict or a CHANGED verdict, with a dedupe key per verdict;
* the "completed" mail went to the whole TA role — it now goes to the
  candidate's own TAs, minus anyone who screens (they get the review mail).

Plus a safety net under every notice in the application: the outbox refuses
the same subject to the same address within `REPEAT_WINDOW_MINUTES`, and the
bell refuses the same title + link to the same user within
`BELL_REPEAT_MINUTES` (messages meant to repeat are exempt).

Run:  cd backend && python -m pytest tests/test_duplicate_notifications.py -q
"""
from __future__ import annotations

from pathlib import Path

from tests.test_screening_notifications import TA, RMG, db  # noqa: F401

BACKEND = Path(__file__).resolve().parents[1]


def test_outbox_collapses_a_repeat_but_never_a_candidate_link(monkeypatch):
    from services import email_outbox as eo

    monkeypatch.setattr(eo, "_safe_scalar", lambda db, sql, params: 7)
    assert eo.is_recent_repeat(None, to_email="a@x.in", subject="S", event="ai_interview.completed")
    # A resent invite / reset link must always go out.
    for event in ("auth.password_reset", "candidate.ai_invite", "candidate.round_invite"):
        assert not eo.is_recent_repeat(None, to_email="a@x.in", subject="S", event=event)
    monkeypatch.setattr(eo, "_safe_scalar", lambda db, sql, params: None)
    assert not eo.is_recent_repeat(None, to_email="a@x.in", subject="S", event="x")


def test_queue_email_consults_the_repeat_guard():
    src = (BACKEND / "services" / "email_outbox.py").read_text(encoding="utf-8")
    body = src.split("def queue_email", 1)[1]
    assert "is_recent_repeat(" in body.split("\ndef ", 1)[0]


def test_the_bell_rings_once_for_the_same_notice(db):  # noqa: F811
    from models import Notification
    from services.notify import notify_roles

    for _ in range(3):
        notify_roles(db, [], "AI L1 passed — review X", "Score 80%", "/admin/?p=x",
                     user_ids=[TA], email=False)
    notify_roles(db, [], "AI L1 passed — review Y", "", "/admin/?p=y",
                 user_ids=[TA], email=False)
    titles = [n.title for n in db.query(Notification).filter(Notification.user_id == TA)]
    assert sorted(titles) == ["AI L1 passed — review X", "AI L1 passed — review Y"]


def test_ai_sync_announces_once_per_verdict_to_the_right_people():
    src = (BACKEND / "services" / "ai_interview_bridge.py").read_text(encoding="utf-8")
    sync = src.split("def sync_completed_interview", 1)[1].split("\ndef ", 1)[0]
    assert "announce = link.completed_at is None or" in sync
    assert "if not announce:" in sync
    assert 'dedupe_prefix=f"ai_done:{link.id}:{new_result}"' in sync
    assert 'dedupe_prefix=f"ai_review:{link.id}:Passed"' in sync
    # Failed and Not attempted are two outcomes with two keys (7 Oct 2026).
    assert 'dedupe_prefix=f"ai_review:{link.id}:{\'not_attempted\' if not_attempted else \'Failed\'}"' in sync
    assert 'dedupe_prefix=f"ai_done:{link.id}:not_attempted"' in sync
    # The TA notice is addressed to the candidate's TAs, not the whole role.
    assert "_candidate_tas(db, link.profile_id)" in sync
    assert 'notify_role(\n                db, "TA"' not in sync
