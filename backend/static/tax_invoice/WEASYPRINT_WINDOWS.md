# WeasyPrint on Windows (Tax Invoice PDF)

The Tax Invoice module prefers **WeasyPrint** (`pip install weasyprint`) to render
the HTML A4 template to PDF.

## Why PDF may fall back

WeasyPrint needs the **GTK3 runtime** (Pango / GObject / Cairo). On this machine
`import weasyprint` fails with:

```
OSError: cannot load library 'libgobject-2.0-0'
```

When that happens, `services/tax_invoice.py::render_pdf` automatically falls back to
**ReportLab** (already a project dependency) and still returns `application/pdf`
with the same API contract (`POST /api/invoice/pdf`,
`GET /api/invoices/{id}/tax-invoice.pdf`, bulk ZIP entries).

Response header `X-Tax-Invoice-Engine` is one of: `weasyprint` | `reportlab` | `html`.

## Install GTK (optional, to enable WeasyPrint)

Follow the official guide:

https://doc.courtbouillon.org/weasyprint/stable/first_steps.html#windows

Typical approach: install the GTK3 runtime for Windows, ensure the GTK `bin`
directory is on `PATH`, then restart the backend.

```bat
pip install weasyprint
python -c "from weasyprint import HTML; HTML(string='<p>ok</p>').write_pdf('t.pdf')"
```

If that writes `t.pdf` without an OSError, WeasyPrint is active.
