"""Import customer branches from a CSV export (companion to replace_customers.py).

Usage (from the backend folder, same venv as the app):

    python tools\import_branches.py ..\import_templates\customer_branches.csv          # DRY RUN (no changes)
    python tools\import_branches.py ..\import_templates\customer_branches.csv --apply  # write to the DB

CSV columns: Customer Name, Branch Name, Billing Address, GSTIN, PAN,
             Customer, Branch Legal Name, ID
(Billing Address is one combined string; city/state/pincode/country are parsed
from its tail, same as replace_customers.py.)

Behaviour:
  * Upsert by (customer, branch name), both matched case-insensitively and
    trimmed: existing branches are UPDATED, new ones are CREATED.
  * Rows whose Customer Name matches no customer are reported and SKIPPED
    (nothing is guessed) - run replace_customers.py first.
  * The first branch of each customer becomes primary if that customer has no
    primary branch yet (single-primary rule preserved).
  * Existing branches not present in the CSV are left untouched.
  * DRY RUN by default; nothing is written without --apply.
"""
from __future__ import annotations

import csv
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # backend/ on sys.path

from sqlalchemy import select

from crm_db import get_session_factory
from models import Customer, CustomerBranch

_PIN_RE = re.compile(r"^\d[\d\s-]{3,9}$")


def parse_address(raw: str) -> dict:
    """Split a combined address string into line1/city/state/pincode/country."""
    parts = [p.strip() for p in (raw or "").split(",")]
    parts = [p for p in parts if p and p != "-"]
    out = {"billing_address": None, "city": None, "state": None,
           "pincode": None, "country": None}
    if not parts:
        return out
    # Last segment is the country - unless it looks like a pincode (address
    # exported without a trailing country, e.g. "..., 600063").
    if _PIN_RE.match(parts[-1]):
        out["pincode"] = re.sub(r"\s+", "", parts[-1])[:16]
        parts = parts[:-1]
    else:
        out["country"] = parts[-1][:120]
        parts = parts[:-1]
    for i in range(len(parts) - 1, max(-1, len(parts) - 4), -1):
        if out["pincode"]:
            break
        if i >= 0 and _PIN_RE.match(parts[i]):
            out["pincode"] = re.sub(r"\s+", "", parts[i])[:16]
            parts = parts[:i] + parts[i + 1:]
            break
    if parts:
        out["state"] = parts[-1][:120]
        parts = parts[:-1]
    if parts:
        out["city"] = parts[-1][:120]
        parts = parts[:-1]
    out["billing_address"] = ", ".join(parts) or None
    return out


def norm(s: str | None) -> str:
    return (s or "").strip().lower()


def clean(s: str | None, limit: int) -> str | None:
    s = (s or "").strip()
    return s[:limit] if s else None


def load_rows(path: Path) -> list[dict]:
    rows: list[dict] = []
    with path.open(newline="", encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            cust = (r.get("Customer Name") or r.get("Customer") or "").strip()
            bname = (r.get("Branch Name") or "").strip()
            if not cust or not bname:
                continue
            rows.append({
                "customer_name": cust,
                "branch_name": bname,
                "branch_legal_name": (r.get("Branch Legal Name") or "").strip() or None,
                "gstin": clean(r.get("GSTIN"), 15),
                "pan": clean(r.get("PAN"), 10),
                **parse_address(r.get("Billing Address") or ""),
            })
    return rows


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    apply = "--apply" in sys.argv
    if not args:
        print(__doc__)
        return 2
    path = Path(args[0])
    if not path.exists():
        print(f"CSV not found: {path}")
        return 2

    rows = load_rows(path)
    print(f"CSV branch rows: {len(rows)}  (mode: {'APPLY' if apply else 'DRY RUN'})\n")

    db = get_session_factory()()
    created, updated, skipped = [], [], []
    try:
        customers = {
            norm(c.name): c for c in db.execute(select(Customer)).scalars().all()
        }
        # Existing branches per customer id, keyed by normalised branch name.
        branches: dict[int, dict[str, CustomerBranch]] = {}
        has_primary: dict[int, bool] = {}
        for b in db.execute(select(CustomerBranch)).scalars().all():
            branches.setdefault(b.customer_id, {})[norm(b.branch_name)] = b
            if b.is_primary:
                has_primary[b.customer_id] = True

        for r in rows:
            cust = customers.get(norm(r["customer_name"]))
            if cust is None:
                skipped.append(f"{r['customer_name']} / {r['branch_name']}")
                continue
            existing = branches.setdefault(cust.id, {}).get(norm(r["branch_name"]))
            fields = {
                "branch_name": r["branch_name"][:255],
                "branch_legal_name": clean(r["branch_legal_name"], 255),
                "billing_address": r["billing_address"],
                "city": r["city"],
                "state": r["state"],
                "pincode": r["pincode"],
                "country": r["country"],
                "gstin": r["gstin"],
                "pan": r["pan"],
            }
            if existing is None:
                make_primary = not has_primary.get(cust.id, False)
                branch = CustomerBranch(customer_id=cust.id, is_primary=make_primary, **fields)
                db.add(branch)
                branches[cust.id][norm(r["branch_name"])] = branch
                if make_primary:
                    has_primary[cust.id] = True
                created.append(f"{cust.name} / {r['branch_name']}"
                               + (" [primary]" if make_primary else ""))
            else:
                for field, value in fields.items():
                    setattr(existing, field, value)
                updated.append(f"{cust.name} / {r['branch_name']}")

        print(f"CREATE  ({len(created)}):")
        for line in created:
            print(f"  + {line}")
        print(f"\nUPDATE  ({len(updated)}):")
        for line in updated:
            print(f"  ~ {line}")
        if skipped:
            print(f"\nSKIPPED - customer not found ({len(skipped)}):")
            for line in skipped:
                print(f"  ! {line}")

        if apply:
            db.commit()
            print("\nAPPLIED - branches imported.")
        else:
            db.rollback()
            print("\nDry run only - nothing changed. Re-run with --apply to write.")
        return 0
    except Exception as exc:
        db.rollback()
        print(f"FAILED - rolled back, nothing changed: {exc}")
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
