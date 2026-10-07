"""The bell's feed stays light (7 Oct 2026).

The bell polls every 30 s from every open tab. It used to re-read the 15-row
page with every message in full plus a COUNT(*) over the user's whole history
on each poll. Now: `since_id` returns only newer rows, no total unless asked,
and `message` is clipped in SQL.

Run:  cd backend && python -m pytest tests/test_notification_feed.py -q
"""
from __future__ import annotations

from types import SimpleNamespace

from tests.test_screening_notifications import TA, RMG, db  # noqa: F401


def _seed(db, n=5, long_message=False):  # noqa: F811
    from models import Notification

    rows = []
    for i in range(n):
        rows.append(Notification(user_id=TA, title=f"N{i}",
                                 message=("x" * 5000) if long_message else f"m{i}", link=f"/p/{i}"))
    db.add(Notification(user_id=RMG, title="someone else's", message="no"))
    db.add_all(rows)
    db.flush()
    return rows


def _call(db, **kw):  # noqa: F811
    from routers.crm.notifications import list_notifications

    kw.setdefault("unread_only", False)
    kw.setdefault("since_id", None)
    kw.setdefault("with_total", False)
    page = SimpleNamespace(page=1, limit=kw.pop("limit", 15))
    return list_notifications(p=page, db=db, user=SimpleNamespace(id=TA), **kw)


def test_poll_returns_only_rows_newer_than_since_id(db):  # noqa: F811
    rows = _seed(db)
    out = _call(db, since_id=rows[2].id)
    assert [r["title"] for r in out["data"]] == ["N4", "N3"]
    assert out["meta"]["unread_count"] == 5
    assert "total" not in out["meta"]                      # no COUNT over the history
    quiet = _call(db, since_id=rows[-1].id)
    assert quiet["data"] == [] and quiet["meta"]["unread_count"] == 5


def test_feed_is_own_rows_only_and_total_on_request(db):  # noqa: F811
    _seed(db, n=3)
    out = _call(db, with_total=True, limit=2)
    assert len(out["data"]) == 2
    assert out["meta"]["total"] == 3                       # only TA's three, not RMG's row
    assert all(not r["title"].startswith("someone") for r in out["data"])


def test_message_is_clipped_to_a_preview(db):  # noqa: F811
    from routers.crm.notifications import MESSAGE_PREVIEW_CHARS

    _seed(db, n=1, long_message=True)
    out = _call(db)
    assert len(out["data"][0]["message"]) == MESSAGE_PREVIEW_CHARS


def test_summary_is_the_badge_count_and_the_top_of_the_feed(db):  # noqa: F811
    from routers.crm.notifications import notification_summary, router

    empty = notification_summary(db=db, user=SimpleNamespace(id=TA))
    assert empty["data"] == {"unread_count": 0, "latest_id": None}
    rows = _seed(db)
    out = notification_summary(db=db, user=SimpleNamespace(id=TA))
    assert out["data"]["unread_count"] == 5
    assert out["data"]["latest_id"] == rows[-1].id          # TA's newest, never RMG's row
    rows[-1].is_read = True
    db.flush()
    assert notification_summary(db=db, user=SimpleNamespace(id=TA))["data"] == {
        "unread_count": 4, "latest_id": rows[-1].id}
    # the literal path is declared before any parametric GET sibling
    gets = [r.path for r in router.routes if "GET" in (getattr(r, "methods", None) or ())]
    literal = gets.index("/api/notifications/summary")
    assert all(i > literal for i, p in enumerate(gets) if "{" in p)
