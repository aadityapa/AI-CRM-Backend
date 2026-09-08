"""The hiring-interest email to Suggested Candidates.

Covers the two things that decide whether a real candidate gets a usable mail:
the {{placeholder}} renderer and the candidate-name helper. Both are pure, so
this file needs no database.

Run:  cd backend && python -m pytest tests/test_candidate_hiring_email.py -q
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from routers.crm.opportunities import (
    CANDIDATE_EMAIL_BODY,
    CANDIDATE_EMAIL_SUBJECT,
    EMAIL_PLACEHOLDERS,
    _candidate_name,
    _real_candidate_email,
    _render_email_template,
)

CTX = {
    "first_name": "Santhukumar",
    "full_name": "Santhukumar Jaisankar",
    "role": "Automation Test Engineer",
    "customer": "Aptiv",
    "sender": "Gargee Joshi",
}


# --- the candidate name -----------------------------------------------------
# Candidate has first_name/last_name and NO `name` column. Reading `.name` is
# the mistake this test exists to catch: it silently yields "Hi there," on every
# email in the batch, which reads as a mail-merge failure to the candidate.

def test_candidate_name_is_built_from_first_and_last():
    c = SimpleNamespace(first_name="Santhukumar", last_name="Jaisankar")
    assert _candidate_name(c) == "Santhukumar Jaisankar"


def test_candidate_name_tolerates_a_missing_last_name():
    assert _candidate_name(SimpleNamespace(first_name="Chetana", last_name=None)) == "Chetana"


def test_candidate_name_is_blank_when_nothing_is_known():
    assert _candidate_name(SimpleNamespace(first_name=None, last_name=None)) == ""


# --- placeholder rendering --------------------------------------------------

def test_default_template_renders_every_placeholder():
    body = _render_email_template(CANDIDATE_EMAIL_BODY, CTX)
    subject = _render_email_template(CANDIDATE_EMAIL_SUBJECT, CTX)
    assert body.startswith("Hi Santhukumar,")
    assert "Automation Test Engineer" in body
    assert body.rstrip().endswith("Gargee Joshi\nKarnex Talent Team")
    assert subject == "Exciting opportunity: Automation Test Engineer"
    # Nothing left unrendered.
    assert "{{" not in body and "{{" not in subject


@pytest.mark.parametrize("token", EMAIL_PLACEHOLDERS)
def test_every_advertised_placeholder_actually_substitutes(token):
    """The composer offers these as clickable chips — each must do something."""
    assert _render_email_template(f"[{{{{{token}}}}}]", CTX) == f"[{CTX[token]}]"


@pytest.mark.parametrize("written", [
    "{{first_name}}", "{{ first_name }}", "{{  first_name  }}",
    "{{first_name }}", "{{ first_name}}",
    "{{First_Name}}", "{{FIRST_NAME}}", "{{fIrSt_NaMe}}",
])
def test_spacing_and_case_are_tolerated(written):
    """A recruiter typing the token by hand will not match our exact casing."""
    assert _render_email_template(f"Hi {written}", CTX) == "Hi Santhukumar"


def test_an_unknown_token_is_left_alone_rather_than_raising():
    """Plain substitution, never str.format — same rule as the 0071 notification
    templates. A typo must cost one odd-looking word, not the whole email."""
    out = _render_email_template("Hi {{first_name}}, re {{not_a_token}} and {0} and {x!r}", CTX)
    assert out == "Hi Santhukumar, re {{not_a_token}} and {0} and {x!r}"


def test_a_stray_brace_is_never_treated_as_a_token():
    assert _render_email_template("Cost { {{role}} } or {{}} or {{ }}", CTX) == (
        "Cost { Automation Test Engineer } or {{}} or {{ }}"
    )


def test_a_substituted_value_is_not_itself_re_scanned():
    """Candidate names come from the PUBLIC apply form. A two-pass renderer
    would let a candidate called "{{sender}}" be greeted with the recruiter's
    name — untrusted data must never become template source."""
    hostile = {**CTX, "first_name": "{{sender}}", "full_name": "{{customer}} {{role}}"}
    assert _render_email_template("Hi {{first_name}}", hostile) == "Hi {{sender}}"
    assert _render_email_template("[{{full_name}}]", hostile) == "[{{customer}} {{role}}]"


def test_an_edited_bulk_message_still_personalises():
    """The reason substitution happens server-side per candidate: one edited
    message, greeting each recipient by their own name."""
    edited = "Hey {{first_name}} — {{customer}} is hiring a {{role}}. — {{sender}}"
    a = _render_email_template(edited, CTX)
    b = _render_email_template(edited, {**CTX, "first_name": "Chetana"})
    assert a == "Hey Santhukumar — Aptiv is hiring a Automation Test Engineer. — Gargee Joshi"
    assert b.startswith("Hey Chetana —")


def test_a_known_token_with_no_value_renders_empty_not_crash():
    """`customer` is a real placeholder, so it substitutes — to nothing when the
    opportunity has no customer name. It must not leak the raw token."""
    assert _render_email_template("[{{customer}}]", {"role": "X", "customer": ""}) == "[]"


def test_a_known_token_absent_from_the_context_is_left_intact():
    """Visible beats blank: the sender can see something went wrong."""
    assert _render_email_template("[{{customer}}]", {"role": "X"}) == "[{{customer}}]"


# --- placeholder e-mail addresses ------------------------------------------

@pytest.mark.parametrize("addr", ["", "   ", "priya.9f3c@import.karnex.in", "A@IMPORT.KARNEX.IN"])
def test_import_placeholders_and_blanks_are_not_mailable(addr):
    assert _real_candidate_email(addr) == ""


def test_a_real_address_survives():
    assert _real_candidate_email("  jsanthu2020@yahoo.com ") == "jsanthu2020@yahoo.com"
