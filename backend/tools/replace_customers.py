"""One-time customer master replace from a CSV export.

Usage (from the backend folder, same venv as the app):

    python tools\\replace_customers.py ..\\import_templates\\Customers.csv          # DRY RUN (no changes)
    python tools\\replace_customers.py ..\\import_templates\\Customers.csv --apply  # write to the DB

CSV columns: Customer Name, Legal Business Name, Address, Status
(Address is one combined string; city/state/pincode/country are parsed from its tail.)

Behaviour:
  * Upsert by customer name (case-insensitive, trimmed): existing customers in the
    CSV are UPDATED (legal name, address, status); new ones are CREATED.
  * Existing customers NOT in the CSV are REMOVED — but only when nothing
    references them. A customer with opportunities / POs / projects /
    requirements is NEVER hard-deleted (that would destroy business history);
    it is marked Inactive instead and listed in the report.
"""
from __future__ import annotations

import csv
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # backend/ on sys.path

from sqlalchemy import func, select

from crm_db import get_session_factory
from models import (
    ContactPerson, Customer, CustomerBranch, CustomerLeavePolicy, CustomerStatus,
    Holiday, Opportunity, Project, PurchaseOrder, Requirement,
)

_PIN_RE = re.compile(r"^\d[\d\s-]{3,9}$")


def parse_address(raw: str) -> dict:
    """Split a combined address string into line1/line2/city/state/pincode/country."""
    parts = [p.strip() for p in (raw or "").split(",")]
    parts = [p for p in parts if p and p != "-"]
    out = {"address_line_1": None, "address_line_2": None, "city": None,
           "state": None, "pincode": None, "country": None}
    if not parts:
        return out
    if len(parts) >= 1:
        out["country"] = parts[-1][:120]
        parts = parts[:-1]
    # pincode: last segment that is digits (allowing internal space/dash)
    for i in range(len(parts) - 1, max(-1, len(parts) - 4), -1):
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
    rest = ", ".join(parts)
    out["address_line_1"] = rest[:255] or None
    if len(rest) > 255:
        out["address_line_2"] = rest[255:510] or None
    return out


def load_rows(path: Path) -> list[dict]:
    rows: list[dict] = []
    with path.open(newline="", encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            name = (r.get("Customer Name") or "").strip()
            if not name:
                continue
            status_raw = (r.get("Status") or "Active").strip().title()
            rows.append({
                "name": name,
                "legal_entity_name": (r.get("Legal Business Name") or "").strip() or None,
                "status": CustomerStatus.INACTIVE if status_raw == "Inactive" else CustomerStatus.ACTIVE,
                **parse_address(r.get("Address") or ""),
            })
    return rows


def refs_count(db, customer_id: int) -> int:
    total = 0
    for model in (Opportunity, PurchaseOrder, Project, Requirement):
        total += db.execute(
            select(func.count()).select_from(model).where(model.customer_id == customer_id)
        ).scalar() or 0
    return total


def delete_customer_tree(db, customer: Customer) -> None:
    """Remove a customer with no business references (branches/contacts/policies/holidays)."""
    branch_ids = [b.id for b in db.execute(
        select(CustomerBranch).where(CustomerBranch.customer_id == customer.id)
    ).scalars().all()]
    db.execute(ContactPerson.__table__.delete().where(ContactPerson.customer_id == customer.id))
    db.execute(CustomerLeavePolicy.__table__.delete().where(CustomerLeavePolicy.customer_id == customer.id))
    db.execute(Holiday.__table__.delete().where(Holiday.customer_id == customer.id))
    if branch_ids:
        from models import BranchHolidayYear
        db.execute(Holiday.__table__.delete().where(Holiday.branch_id.in_(branch_ids)))
        db.execute(BranchHolidayYear.__table__.delete().where(BranchHolidayYear.branch_id.in_(branch_ids)))
        db.execute(CustomerBranch.__table__.delete().where(CustomerBranch.id.in_(branch_ids)))
    db.delete(customer)


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
    print(f"CSV rows: {len(rows)}  (mode: {'APPLY' if apply else 'DRY RUN'})\n")

    db = get_session_factory()()
    created, updated, deleted, deactivated = [], [], [], []
    try:
        existing = {
            (c.name or "").strip().lower(): c
            for c in db.execute(select(Customer)).scalars().all()
        }
        seen: set[str] = set()
        for r in rows:
            key = r["name"].lower()
            seen.add(key)
            cust = existing.get(key)
            if cust is None:
                db.add(Customer(**r))
                created.append(r["name"])
            else:
                for field, value in r.items():
                    if field == "name":
                        continue
                    setattr(cust, field, value)
                updated.append(r["name"])

        for key, cust in existing.items():
            if key in seen:
                continue
            if refs_count(db, cust.id) > 0:
                if cust.status != CustomerStatus.INACTIVE:
                    cust.status = CustomerStatus.INACTIVE
                deactivated.append(cust.name)
            else:
                delete_customer_tree(db, cust)
                deleted.append(cust.name)

        print(f"CREATE  ({len(created)}): {', '.join(created) or '—'}\n")
        print(f"UPDATE  ({len(updated)}): {', '.join(updated) or '—'}\n")
        print(f"DELETE  ({len(deleted)}): {', '.join(deleted) or '—'}\n")
        print(f"KEEP AS INACTIVE — has opportunities/POs/projects ({len(deactivated)}): "
              f"{', '.join(deactivated) or '—'}\n")

        if apply:
            db.commit()
            print("APPLIED ✔ — customer master replaced.")
        else:
            db.rollback()
            print("Dry run only — nothing changed. Re-run with --apply to write.")
        return 0
    except Exception as exc:
        db.rollback()
        print(f"FAILED — rolled back, nothing changed: {exc}")
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
