"""The application is called "Karnex Orbit" (26 Sep 2026).

`config.APP_NAME` is the ONE product name; every user-facing surface reads it.
"Karnex" alone remains the COMPANY (invoices, offer letters, the recruitment
team sign-off) — this test only forbids the OLD PRODUCT names from creeping
back into strings a person sees.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from config import APP_NAME, APP_TITLE

BACKEND = Path(__file__).resolve().parents[1]

#: Old product names, none of which may appear in a user-facing string again.
OLD_NAMES = (
    "Karnex CRM", "KARNEX CRM", "AI HR Suite", "KARNEX AI HR", "Karnex AI HR",
    "AI Interview Demo", "AI Assessment Center", "AI Hiring OS",
)

#: Modules whose STRING LITERALS reach users (mail, prompts, PDFs, errors).
USER_FACING_MODULES = (
    "config.py", "crm_db.py", "email_smtp.py",
    "ai_help/assist.py", "services/support.py", "services/candidate_comms.py",
    "services/email_outbox.py", "services/invoice_pdf.py", "services/data_backup.py",
    "services/ai_interview_bridge.py", "services/interview_invite_email.py",
)


def _string_literals(source: str) -> str:
    """Every quoted literal in the file, comments and docstrings stripped."""
    body = re.sub(r'"""[\s\S]*?"""|\'\'\'[\s\S]*?\'\'\'', "", source)
    body = "\n".join(line.split("#", 1)[0] for line in body.splitlines())
    return " ".join(re.findall(r'"[^"\n]*"|\'[^\'\n]*\'', body))


def test_the_app_name_is_karnex_orbit():
    assert APP_NAME == "Karnex Orbit"
    assert APP_TITLE == APP_NAME  # main.py still imports the older alias


@pytest.mark.parametrize("module", USER_FACING_MODULES)
def test_no_old_product_name_in_user_facing_strings(module):
    literals = _string_literals((BACKEND / module).read_text(encoding="utf-8"))
    for old in OLD_NAMES:
        assert old not in literals, f"{module} still says {old!r} — use config.APP_NAME"


def test_the_api_and_health_report_the_app_name():
    import main
    assert main.app.title == APP_NAME


def test_mail_signs_off_with_the_app_name(monkeypatch):
    import email_smtp

    captured: dict = {}
    monkeypatch.setattr(email_smtp, "send_email",
                        lambda to, subject, text, html, **kw: captured.update(
                            to=to, subject=subject, text=text, html=html) or {"ok": True})
    email_smtp.send_interview_invite_email("c@x.in", "Cand", "https://x/?invite=t", "1 Oct")
    assert APP_NAME in captured["subject"]
    assert captured["text"].rstrip().endswith(f"— {APP_NAME}")
    assert f"— {APP_NAME}" in captured["html"]


def test_outbox_from_label_is_the_app_name():
    from services import email_outbox
    assert email_outbox._DEFAULT_FROM_LABEL == APP_NAME
