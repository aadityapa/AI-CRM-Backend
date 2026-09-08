"""Karnex - put the logged-in user in the From header.

Before: From: "Pavan Sanap (Karnex)" <noreply@karnex.in>, Reply-To: pavan.sanap@...
After:  From: "Pavan Sanap (Karnex)" <pavan.sanap@karnex.in>

Only possible since the move to ZeptoMail - see the comment the patch inserts.

    cd F:\\AI-Interview-Model-B-V2
    python karnex_sender_identity.py --check   dry run
    python karnex_sender_identity.py           apply

Safe to re-run. One file patched, one .env key added. No migration.
"""
from __future__ import annotations

import argparse
import io
import os

OK, BAD, WARN = "  [ok]  ", "  [FAIL]", "  [warn]"
CHECK_ONLY = False


def repo_root() -> str:
    here = os.path.abspath(os.getcwd())
    if os.path.isfile(os.path.join(here, "backend", "main.py")):
        return here
    parent = os.path.dirname(here)
    if os.path.isfile(os.path.join(parent, "backend", "main.py")):
        return parent
    print("ERROR: run this from F:\\AI-Interview-Model-B-V2 (or its backend folder).")
    raise SystemExit(2)


def _read(path: str) -> str:
    with io.open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def _write(path: str, text: str) -> None:
    if CHECK_ONLY:
        return
    with io.open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


OLD_BLOCK = '''    msg["Subject"] = subject
    display = (from_name or "").strip()
    if display:
        from email.utils import formataddr

        msg["From"] = formataddr((display, from_addr))
    else:
        msg["From"] = from_addr
    msg["To"] = to_address
    reply_addr = (reply_to or "").strip()
    if reply_addr and reply_addr.lower() != from_addr.lower():
        from email.utils import formataddr

        rname = (reply_to_name or "").strip()
        msg["Reply-To"] = formataddr((rname, reply_addr)) if rname else reply_addr'''

NEW_BLOCK = '''    msg["Subject"] = subject

    # Send AS the person whose action triggered this, when their address sits on
    # a domain the provider has verified for us.
    #
    # This only became possible on ZeptoMail. Office 365 rejects a From: of
    # another mailbox with "5.7.60 Client does not have permissions to send as
    # this sender" unless an admin grants SendAs per mailbox; a provider that
    # verifies a whole DOMAIN accepts any address on it, with no per-user setup.
    #
    # The ENVELOPE sender deliberately stays SMTP_FROM. Only the visible From
    # header changes. That keeps bounces and complaints landing on one monitored
    # address instead of scattering across every recruiter's mailbox, and it is
    # the ordinary arrangement for application mail.
    #
    # EMAIL_SENDER_DOMAINS is the guard. An address outside it falls back to
    # SMTP_FROM rather than attempting a send the provider would refuse — so an
    # external or malformed address degrades quietly instead of bouncing.
    from email.utils import formataddr

    allowed_domains = {
        d.strip().lower().lstrip("@")
        for d in (os.getenv("EMAIL_SENDER_DOMAINS") or "").split(",")
        if d.strip()
    }
    actor = (reply_to or "").strip()
    actor_domain = actor.rsplit("@", 1)[-1].lower() if "@" in actor else ""
    send_as = actor if (actor and actor_domain in allowed_domains) else from_addr

    display = (from_name or "").strip()
    msg["From"] = formataddr((display, send_as)) if display else send_as
    msg["To"] = to_address
    # Reply-To is redundant once From IS the actor; only add it when they differ.
    if actor and actor.lower() != send_as.lower():
        rname = (reply_to_name or "").strip()
        msg["Reply-To"] = formataddr((rname, actor)) if rname else actor'''


def patch_email_smtp(root: str) -> bool:
    print("=== 1. Sender identity (email_smtp.py) ===")
    path = os.path.join(root, "backend", "email_smtp.py")
    if not os.path.exists(path):
        print(BAD + " backend/email_smtp.py not found")
        return False
    text = _read(path)
    if "EMAIL_SENDER_DOMAINS" in text:
        print("  already applied")
        return True
    if text.count(OLD_BLOCK) != 1:
        print(BAD + " anchor not found (" + str(text.count(OLD_BLOCK)) + " matches).")
        print("       email_smtp.py must already have the from_name / reply_to support")
        print("       added by apply_email_notifications.py. If it does not, run that first.")
        return False
    _write(path, text.replace(OLD_BLOCK, NEW_BLOCK))
    print("  " + ("would patch" if CHECK_ONLY else "patched") + "  backend/email_smtp.py")
    return True


def configure_env(root: str) -> None:
    print("")
    print("=== 2. Allowed sender domains (.env) ===")
    path = os.path.join(root, ".env")
    if not os.path.exists(path):
        print(WARN + " no .env found — skipping")
        return
    lines = _read(path).splitlines()
    for i, line in enumerate(lines):
        s = line.strip()
        if s and not s.startswith("#") and "=" in s and s.split("=", 1)[0].strip() == "EMAIL_SENDER_DOMAINS":
            print(OK + " EMAIL_SENDER_DOMAINS already set: " + s.split("=", 1)[1].strip())
            return
    if lines and lines[-1].strip():
        lines.append("")
    lines.append("# Domains we may put in the From header. Anything else falls back to")
    lines.append("# SMTP_FROM. Must be a domain verified with your mail provider.")
    lines.append("EMAIL_SENDER_DOMAINS=karnex.in")
    _write(path, "\n".join(lines) + "\n")
    print("  added  EMAIL_SENDER_DOMAINS=karnex.in")


def show_effect(root: str) -> None:
    print("")
    print("=== 3. What changes ===")
    print("")
    print("  Before:")
    print('    From:     "Pavan Sanap (Karnex)" <noreply@karnex.in>')
    print("    Reply-To: pavan.sanap@karnex.in")
    print("")
    print("  After:")
    print('    From:     "Pavan Sanap (Karnex)" <pavan.sanap@karnex.in>')
    print("    (no Reply-To — it would just repeat the From)")
    print("")
    print("  Envelope sender stays " + (os.getenv("SMTP_FROM") or "SMTP_FROM") +
          " so bounces stay in one place.")


def main() -> int:
    global CHECK_ONLY
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="dry run, change nothing")
    args = ap.parse_args()
    CHECK_ONLY = args.check

    root = repo_root()
    print("Karnex — send as the logged-in user")
    print("")
    print("Repo: " + root)
    print("")

    ok = patch_email_smtp(root)
    configure_env(root)
    show_effect(root)

    print("")
    print("=" * 62)
    if not ok:
        print("NOT APPLIED — see above. Paste this output back to Claude.")
        return 1
    if CHECK_ONLY:
        print("Dry run only — nothing written. Re-run without --check to apply.")
        return 0
    print("Applied. Restart the backend, then submit a timesheet and check the From.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())