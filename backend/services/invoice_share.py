"""Public "scan to view" links for Tax Invoices (3 Sep 2026, user request).

The printed invoice carries a QR code beside the seal. Whoever scans it —
the customer's AP team, an auditor, anyone with the paper — opens the
complete invoice on their phone without a Karnex login. That needs three
things, all here:

  * a SIGNED TOKEN per invoice (`{id}.{hmac}`) so the public URL cannot be
    guessed or enumerated — the HMAC is keyed on AUTH_SECRET, the same secret
    every session token trusts, so rotating it invalidates every printed QR
    at once (acceptable: reprint);
  * the PUBLIC URLs — this app's own HTML page / JSON / PDF under
    `/api/public/invoices/{token}/…`, plus the QR TARGET, which defaults to
    the HTML page but can be pointed at the hosted Karnex Invoice Viewer
    (Vercel) through the `invoice.qr_viewer_url` setting;
  * the QR image itself, rendered with reportlab's pure-Python QR widget as
    an SVG data URL — no new dependency, and an <img src="data:…"> is what
    both the React sheet (html2canvas PDF) and the WeasyPrint template embed.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging

logger = logging.getLogger("karnex.crm.invoice_share")

_SCOPE = b"karnex-invoice-share:"


def _secret() -> bytes:
    try:
        from auth_secret import auth_secret
        return auth_secret().encode("utf-8")
    except Exception:  # tests without AUTH_SECRET configured
        return b"karnex-dev-invoice-share"


def _sig(invoice_id: int) -> str:
    return hmac.new(_secret(), _SCOPE + str(int(invoice_id)).encode(), hashlib.sha256).hexdigest()[:24]


def share_token(invoice_id: int) -> str:
    """`{id}.{24-hex hmac}` — stable for the invoice's lifetime."""
    return f"{int(invoice_id)}.{_sig(invoice_id)}"


def parse_share_token(token: str | None) -> int | None:
    """The invoice id a token names, or None when malformed / forged."""
    raw = (token or "").strip()
    if "." not in raw:
        return None
    left, _, sig = raw.partition(".")
    if not left.isdigit() or not sig:
        return None
    invoice_id = int(left)
    if not hmac.compare_digest(sig, _sig(invoice_id)):
        return None
    return invoice_id


def public_base_url(fallback: str = "") -> str:
    """The externally reachable origin: the Settings-page value
    (`email.public_base_url`) first, else the caller's request origin."""
    try:
        from services.email_outbox import app_url
        base = app_url("")
    except Exception:
        base = ""
    return (base or fallback or "").rstrip("/")


def share_links(invoice_id: int, invoice_number: str | None, *, base_url: str = "") -> dict:
    """Every public URL for an invoice + the one the QR encodes."""
    token = share_token(invoice_id)
    base = public_base_url(base_url)
    root = f"{base}/api/public/invoices/{token}"
    links = {
        "token": token,
        "view_url": f"{root}/view",
        "data_url": root,
        "pdf_url": f"{root}/pdf",
    }
    raw = ""
    try:
        from services.org_settings import setting
        raw = (setting("invoice.qr_viewer_url") or "").strip()
    except Exception:
        raw = ""
    target = raw
    if raw:
        for key, value in {
            "view_url": links["view_url"], "pdf_url": links["pdf_url"], "data_url": links["data_url"],
            "token": token, "invoice_number": invoice_number or "", "id": str(invoice_id),
        }.items():
            target = target.replace("{" + key + "}", value)
        if "{" not in raw:
            # A bare viewer origin with no placeholder gets the JSON URL appended
            # the way most static viewers expect (?src=…).
            sep = "&" if "?" in target else "?"
            target = f"{target}{sep}src={links['data_url']}"
    links["qr_target"] = target or links["view_url"]
    return links


def qr_svg_data_url(text: str, size_px: int = 220) -> str | None:
    """A QR code for `text` as an SVG data URL (reportlab, no PIL needed)."""
    if not text:
        return None
    try:
        from reportlab.graphics import renderSVG
        from reportlab.graphics.barcode.qr import QrCodeWidget
        from reportlab.graphics.shapes import Drawing

        widget = QrCodeWidget(text, barLevel="M")
        x0, y0, x1, y1 = widget.getBounds()
        w, h = (x1 - x0) or 1, (y1 - y0) or 1
        drawing = Drawing(size_px, size_px, transform=[size_px / w, 0, 0, size_px / h, 0, 0])
        drawing.add(widget)
        svg = renderSVG.drawToString(drawing)
        return "data:image/svg+xml;base64," + base64.b64encode(svg.encode("utf-8")).decode("ascii")
    except Exception:  # pragma: no cover — a missing QR must never break the invoice
        logger.warning("QR render failed", exc_info=True)
        return None
