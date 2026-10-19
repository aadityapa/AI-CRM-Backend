"""PUBLIC "remove me from your talent pool" page (17 Oct 2026) — the link at the
foot of every candidate email (services/talent_retention.email_footer).

    GET  /api/public/talent-pool/opt-out/{token}   a page with one button
    POST /api/public/talent-pool/opt-out/{token}   take me out (idempotent)

NO LOGIN. The token is `{candidate id}.{hmac}` keyed on AUTH_SECRET (scope
"karnex-pool-optout:"), so it cannot be guessed or enumerated, and it reveals
nothing: the page greets the candidate by FIRST name only — never email, phone
or any application. GET never changes anything (mail scanners pre-fetch links);
only the button's POST does. Rate-limited like the other public endpoints.
"""
from __future__ import annotations

from html import escape

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

import rate_limit as _rl
from crm_deps import get_crm_db
from models import Candidate
from services import talent_retention as retention

router = APIRouter(prefix="/api/public/talent-pool", tags=["Public: Talent Pool opt-out"])


def _page(title: str, body: str, status: int = 200) -> HTMLResponse:
    html = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow"><title>{escape(title)}</title>
<style>
body{{margin:0;font-family:'Segoe UI',Arial,sans-serif;background:#f1f5f9;color:#1e293b}}
.card{{max-width:480px;margin:48px auto;background:#fff;border-radius:16px;padding:28px 26px;
box-shadow:0 10px 30px rgba(15,23,42,.08)}}
h1{{font-size:20px;margin:0 0 12px}} p{{line-height:1.6;font-size:15px;color:#334155}}
.brand{{font-weight:800;letter-spacing:-.02em;margin-bottom:18px}} .brand b{{color:#2563eb}}
button{{background:#e11d48;color:#fff;border:0;border-radius:10px;padding:12px 18px;font-size:15px;
font-weight:700;cursor:pointer}} .muted{{font-size:13px;color:#64748b}}
</style></head><body><div class="card"><div class="brand">KARNEX <b>Careers</b></div>{body}</div></body></html>"""
    return HTMLResponse(html, status_code=status)


def _invalid() -> HTMLResponse:
    return _page("Link not valid", "<h1>This link is not valid</h1><p>It may have been copied incompletely. "
                 "Reply to any email from our recruiters and we will remove you by hand.</p>", 404)


def _candidate(db: Session, token: str) -> Candidate | None:
    cid = retention.parse_opt_out_token(token)
    return db.get(Candidate, cid) if cid is not None else None


@router.get("/opt-out/{token}", response_class=HTMLResponse)
def opt_out_page(token: str, db: Session = Depends(get_crm_db)):
    cand = _candidate(db, token)
    if cand is None:
        return _invalid()
    first = escape((cand.first_name or "there").strip() or "there")
    if cand.pool_opt_out_at is not None:
        return _page("Removed", f"<h1>You are already removed, {first}</h1><p>We will not contact you about "
                     "new openings. Nothing else to do.</p>")
    return _page("Leave our talent pool", f"""
<h1>Hello {first},</h1>
<p>You are in Karnex's talent pool: when a role that fits your experience opens up, a recruiter may get in
touch. If you would rather not hear about new openings, press the button below.</p>
<form method="post"><button type="submit">Remove me from the talent pool</button></form>
<p class="muted">If you are in an interview process with us right now, you will still receive messages about
that application.</p>""")


@router.post("/opt-out/{token}", response_class=HTMLResponse)
@_rl.limit("10/minute")
def opt_out_confirm(request: Request, token: str, db: Session = Depends(get_crm_db)):  # noqa: ARG001 — slowapi needs `request`
    cand = _candidate(db, token)
    if cand is None:
        return _invalid()
    changed = retention.opt_out(db, cand, source="candidate", reason="Used the link in an email", user_id=None)
    if changed:
        retention.notify_opt_out(db, cand)
    db.commit()
    first = escape((cand.first_name or "there").strip() or "there")
    return _page("Removed", f"<h1>Done, {first}</h1><p>You have been removed from our talent pool. We will not "
                 "contact you about new openings. If you change your mind, just reply to any of our emails.</p>")
