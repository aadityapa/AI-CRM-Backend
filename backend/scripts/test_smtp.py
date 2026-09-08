"""Prove SMTP works before testing the AI interview flow in the UI.

Sends a real email using the same config the app uses (.env → SMTP_*), and
reports exactly which step failed if it doesn't go out. Run this FIRST — if this
fails, the "Schedule AI interview" email will fail the same way.

Usage:
    python scripts/test_smtp.py                      # send to SMTP_USER (yourself)
    python scripts/test_smtp.py someone@example.com  # send to a specific address
    python scripts/test_smtp.py --check              # config + connection only, no send
"""
from __future__ import annotations

import os
import smtplib
import ssl
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

def _load_env() -> None:
    """Same loader main.py uses — the repo has no python-dotenv dependency."""
    env_path = BACKEND.parent / ".env"
    if not env_path.exists():
        print(f"WARNING: no .env at {env_path}")
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


_load_env()

from email_smtp import send_email, smtp_configured, smtp_enabled  # noqa: E402

MASK = lambda v: ("*" * len(v)) if v else "(empty)"  # noqa: E731


def main() -> int:
    argv = sys.argv[1:]
    check_only = "--check" in argv
    to = next((a for a in argv if not a.startswith("--")), "")

    host = (os.getenv("SMTP_HOST") or "").strip()
    port = int((os.getenv("SMTP_PORT") or "587").strip() or "587")
    user = (os.getenv("SMTP_USER") or "").strip()
    pwd = (os.getenv("SMTP_PASSWORD") or "").strip()
    sender = (os.getenv("SMTP_FROM") or user).strip()
    use_tls = (os.getenv("SMTP_USE_TLS", "true").strip().lower() in {"1", "true", "yes", "on"})
    to = to or user

    print("SMTP configuration")
    print(f"  SMTP_ENABLED  : {os.getenv('SMTP_ENABLED')}  -> enabled={smtp_enabled()}")
    print(f"  SMTP_HOST     : {host}")
    print(f"  SMTP_PORT     : {port}")
    print(f"  SMTP_USER     : {user}")
    print(f"  SMTP_PASSWORD : {MASK(pwd)}")
    print(f"  SMTP_FROM     : {sender}")
    print(f"  SMTP_USE_TLS  : {use_tls}")
    print(f"  configured    : {smtp_configured()}")
    print(f"  AI_INTERVIEW_AUTOSEND : {os.getenv('AI_INTERVIEW_AUTOSEND')}")
    print(f"  PUBLIC_BASE_URL       : {os.getenv('PUBLIC_BASE_URL')}\n")

    if not smtp_enabled():
        print("FAIL: SMTP_ENABLED is not true — the app will skip every email.")
        return 1
    if not smtp_configured():
        print("FAIL: SMTP_HOST / SMTP_USER / SMTP_PASSWORD are not all set.")
        return 1

    # ---- connection + auth, reported separately from the send -------------
    print(f"Connecting to {host}:{port} …")
    try:
        if use_tls and port == 465:
            server = smtplib.SMTP_SSL(host, port, context=ssl.create_default_context(), timeout=30)
        else:
            server = smtplib.SMTP(host, port, timeout=30)
        with server:
            server.ehlo()
            if use_tls and port != 465:
                server.starttls(context=ssl.create_default_context())
                server.ehlo()
            print("  connection + TLS OK")
            server.login(user, pwd)
            print("  authentication OK")
    except smtplib.SMTPAuthenticationError as err:
        print(f"\nFAIL: authentication rejected — {err}")
        print("\n  Microsoft 365 rejects plain passwords when:")
        print("   • SMTP AUTH is disabled for the tenant or the mailbox")
        print("     (Microsoft 365 admin centre → Users → Mail → Manage email apps")
        print("      → tick 'Authenticated SMTP')")
        print("   • the account has MFA — you then need an App Password, not the")
        print("     normal sign-in password")
        print("   • security defaults / conditional access block legacy auth")
        print("\n  Ask IT to enable Authenticated SMTP for this mailbox, or switch to")
        print("  Microsoft Graph / a transactional provider (SendGrid, SES, Postmark).")
        return 1
    except Exception as err:
        print(f"\nFAIL: could not connect — {type(err).__name__}: {err}")
        print("  Check the host/port, and whether outbound port 587 is blocked here.")
        return 1

    if check_only:
        print("\nConfig and authentication are fine (--check: no email sent).")
        return 0

    print(f"\nSending test email to {to} …")
    result = send_email(
        to,
        "Karnex — SMTP test",
        "This is a test from the Karnex CRM.\n\n"
        "If you are reading this, AI interview invites will reach candidates.\n\n"
        "— Karnex\n",
        '<div style="font-family:Segoe UI,Arial,sans-serif;line-height:1.6;color:#1e293b;">'
        "<p>This is a test from the <strong>Karnex CRM</strong>.</p>"
        "<p>If you are reading this, AI interview invites will reach candidates.</p>"
        "<p>— Karnex</p></div>",
    )
    if result.get("ok"):
        print(f"\nSENT. Check the inbox for {to} (and the junk folder).")
        return 0
    print(f"\nFAIL: {result.get('error')}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
