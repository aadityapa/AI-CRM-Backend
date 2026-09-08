"""Outgoing mail is sent as the logged-in user (8 Sep 2026, user request).

Pins the request-scoped actor holder: the middleware opens it, the
`get_current_user` dependency fills it from a threadpool copy of the context,
and a sync endpoint (another copy) plus the mail layer read the same user.
Also pins the SMTP From selection: an actor on SMTP_FROM's own domain sends
as themselves; an outside address falls back to SMTP_FROM with Reply-To.
"""
from __future__ import annotations

import os
import smtplib
from types import SimpleNamespace

import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient

import email_smtp
from services import actor_context


def test_actor_visible_to_sync_endpoint_and_mail_layer():
    app = FastAPI()

    @app.middleware("http")
    async def scope(request: Request, call_next):
        actor_context.begin_request()
        return await call_next(request)

    def dep():   # sync → threadpool copy, like crm_deps.get_current_user
        actor_context.set_current_actor(SimpleNamespace(id=7, full_name="Gargee Joshi",
                                                        email="gargee.joshi@karnex.in"))
        return True

    @app.get("/x")
    def endpoint(_=Depends(dep)):   # sync → another threadpool copy
        a = actor_context.current_actor()
        return {"who": getattr(a, "email", None)}

    @app.get("/y")
    def other():
        return {"who": getattr(actor_context.current_actor(), "email", None)}

    c = TestClient(app)
    assert c.get("/x").json() == {"who": "gargee.joshi@karnex.in"}
    # A request that never authenticates sees nobody — no leak between requests.
    assert c.get("/y").json() == {"who": None}


def _capture_send(monkeypatch, refuse_send_as: bool = False):
    sent: list[dict] = []

    class FakeSMTP:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def starttls(self, **k): pass
        def login(self, u, p): pass
        def sendmail(self, envelope_from, to, data):
            from email import message_from_string
            from email.utils import parseaddr
            m = message_from_string(data)
            if refuse_send_as and "noreply@karnex.in" not in m["From"]:
                raise smtplib.SMTPDataError(550, b"5.7.60 SMTP; Client does not have permissions to send as this sender")
            sent.append({"envelope": envelope_from, "from": parseaddr(m["From"]),
                         "reply_to": parseaddr(m["Reply-To"]) if m["Reply-To"] else None})

    monkeypatch.setattr(email_smtp.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(email_smtp.smtplib, "SMTP_SSL", FakeSMTP)
    for k, v in {"SMTP_ENABLED": "true", "SMTP_HOST": "smtp.test", "SMTP_USER": "noreply@karnex.in",
                 "SMTP_PASSWORD": "x", "SMTP_FROM": "noreply@karnex.in", "SMTP_PORT": "587"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("EMAIL_SENDER_DOMAINS", raising=False)
    monkeypatch.setattr("services.org_settings.setting", lambda key, default=None: "", raising=False)
    return sent


def test_same_domain_actor_is_the_from_address(monkeypatch):
    sent = _capture_send(monkeypatch)
    r = email_smtp.send_email("cand@example.com", "Hi", "body", from_name="Gargee Joshi (Karnex)",
                              reply_to="gargee.joshi@karnex.in", reply_to_name="Gargee Joshi")
    assert r["ok"], r
    assert sent[0]["from"] == ("Gargee Joshi (Karnex)", "gargee.joshi@karnex.in")
    assert sent[0]["envelope"] == "noreply@karnex.in"      # bounces stay on the monitored box
    assert sent[0]["reply_to"] is None                      # redundant once From IS the actor


def test_outside_domain_falls_back_to_shared_sender(monkeypatch):
    sent = _capture_send(monkeypatch)
    r = email_smtp.send_email("cand@example.com", "Hi", "body", from_name="Ext (Karnex)",
                              reply_to="someone@gmail.com")
    assert r["ok"], r
    assert sent[0]["from"] == ("Ext (Karnex)", "noreply@karnex.in")
    assert sent[0]["reply_to"] == ("", "someone@gmail.com")


def test_provider_refusing_send_as_retries_from_shared_sender(monkeypatch):
    sent = _capture_send(monkeypatch, refuse_send_as=True)
    r = email_smtp.send_email("cand@example.com", "Hi", "body", from_name="Gargee Joshi (Karnex)",
                              reply_to="gargee.joshi@karnex.in", reply_to_name="Gargee Joshi")
    assert r["ok"], r
    assert sent[0]["from"] == ("Gargee Joshi (Karnex)", "noreply@karnex.in")
    assert sent[0]["reply_to"] == ("Gargee Joshi", "gargee.joshi@karnex.in")
