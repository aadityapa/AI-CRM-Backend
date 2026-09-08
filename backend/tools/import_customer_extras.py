"""Import contact persons, contact roles, and branch holiday-year headers.

Companion to replace_customers.py / import_branches.py - run those first so
customers and branches exist.

Usage (from the backend folder, same venv as the app):

    python tools\import_customer_extras.py                 # DRY RUN, all three CSVs
    python tools\import_customer_extras.py --apply         # write to the DB

Optional flags to limit what runs (default: all three, using the files in
..\import_templates):
    --contacts [path]    customer_contacts.csv       -> contact_persons
    --roles [path]       contact_roles.csv           -> contact_roles master
    --holidays [path]    branch_holiday_years.csv    -> branch_holiday_years

Behaviour:
  * Contacts upsert by (customer, branch, contact name), case-insensitive.
    Duplicate CSV rows for the same person are merged; non-empty values win.
    A comma-separated "Customer Branch" cell creates one contact per branch.
    CSV "Department" fills the contact's role field.
  * Roles upsert the contact_roles master by name.
  * Holidays create/update the per-branch calendar-year headers only (the
    holiday DATES are not in this CSV; add them in the UI or a separate import).
  * Rows referencing unknown customers/branches are reported and SKIPPED.
  * DRY RUN by default; nothing is written without --apply.
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # backend/ on sys.path

from sqlalchemy import select

from crm_db import get_session_factory
from models import BranchHolidayYear, ContactPerson, ContactRole, Customer, CustomerBranch

TEMPLATES = Path(__file__).resolve().parent.parent.parent / "import_templates"


def norm(s: str | None) -> str:
    return " ".join((s or "").split()).lower()


def clean(s: str | None, limit: int) -> str | None:
    s = " ".join((s or "").split())
    return s[:limit] if s else None


def read_csv(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


# ---------------------------------------------------------------------------
# Contacts
# ---------------------------------------------------------------------------

def merge_contact_rows(rows: list[dict]) -> list[dict]:
    """Split multi-branch cells, then merge duplicate (customer, branch, name)
    rows field-wise - later non-empty values overwrite earlier ones."""
    expanded: list[dict] = []
    for r in rows:
        name = (r.get("Name") or "").strip()
        cust = (r.get("Customers") or "").strip()
        if not name or not cust:
            continue
        branch_cell = (r.get("Customer Branch") or "").strip()
        branch_names = [b.strip() for b in branch_cell.split(",") if b.strip()] or [""]
        for bname in branch_names:
            expanded.append({
                "customer": cust,
                "branch": bname,
                "name": " ".join(name.split()),
                "email": clean(r.get("Email"), 255),
                "phone": clean(r.get("Phone"), 32),
                "role": clean(r.get("Department"), 40),
                "contact_priority": clean(r.get("Primary / Secondary"), 40),
                "notification": clean(r.get("Notification"), 40),
            })
    merged: dict[tuple, dict] = {}
    for r in expanded:
        key = (norm(r["customer"]), norm(r["branch"]), norm(r["name"]))
        cur = merged.get(key)
        if cur is None:
            merged[key] = dict(r)
        else:
            for f in ("email", "phone", "role", "contact_priority", "notification"):
                if r[f]:
                    cur[f] = r[f]
    return list(merged.values())


def import_contacts(db, path: Path, out: list[str]) -> tuple[int, int]:
    rows = merge_contact_rows(read_csv(path))
    customers = {norm(c.name): c for c in db.execute(select(Customer)).scalars().all()}
    branches: dict[int, dict[str, CustomerBranch]] = {}
    for b in db.execute(select(CustomerBranch)).scalars().all():
        branches.setdefault(b.customer_id, {})[norm(b.branch_name)] = b
    existing: dict[tuple, ContactPerson] = {}
    for c in db.execute(select(ContactPerson)).scalars().all():
        existing[(c.customer_id, c.branch_id, norm(c.name))] = c

    created = updated = 0
    for r in rows:
        cust = customers.get(norm(r["customer"]))
        if cust is None:
            out.append(f"  ! contact skipped, unknown customer: {r['customer']} / {r['name']}")
            continue
        branch = None
        if r["branch"]:
            branch = branches.get(cust.id, {}).get(norm(r["branch"]))
            if branch is None:
                out.append(f"  ! contact skipped, unknown branch: {r['customer']} / {r['branch']} / {r['name']}")
                continue
        branch_id = branch.id if branch is not None else None
        key = (cust.id, branch_id, norm(r["name"]))
        fields = {
            "name": r["name"][:255],
            "email": r["email"],
            "phone": r["phone"],
            "role": r["role"],
            "contact_priority": r["contact_priority"],
            "notification": r["notification"],
        }
        row = existing.get(key)
        if row is None:
            row = ContactPerson(customer_id=cust.id, branch_id=branch_id,
                                is_active=True, **fields)
            db.add(row)
            existing[key] = row
            created += 1
            out.append(f"  + contact: {cust.name} / {r['branch'] or '(customer-level)'} / {r['name']}")
        else:
            for f, v in fields.items():
                if v:
                    setattr(row, f, v)
            updated += 1
            out.append(f"  ~ contact: {cust.name} / {r['branch'] or '(customer-level)'} / {r['name']}")
    return created, updated


# ---------------------------------------------------------------------------
# Contact roles master
# ---------------------------------------------------------------------------

def import_roles(db, path: Path, out: list[str]) -> tuple[int, int]:
    names = []
    for r in read_csv(path):
        name = " ".join((r.get("Role") or "").split())
        if name and norm(name) not in [norm(n) for n in names]:
            names.append(name)
    existing = {norm(r.name): r for r in db.execute(select(ContactRole)).scalars().all()}
    created = updated = 0
    for name in names:
        row = existing.get(norm(name))
        if row is None:
            db.add(ContactRole(name=name[:120], is_active=True))
            created += 1
            out.append(f"  + role: {name}")
        else:
            if not row.is_active:
                row.is_active = True
            updated += 1
            out.append(f"  ~ role: {name} (already present)")
    return created, updated


# ---------------------------------------------------------------------------
# Branch holiday-year headers
# ---------------------------------------------------------------------------

def import_holiday_years(db, path: Path, out: list[str]) -> tuple[int, int]:
    by_name: dict[str, list[CustomerBranch]] = {}
    for b in db.execute(select(CustomerBranch)).scalars().all():
        by_name.setdefault(norm(b.branch_name), []).append(b)
    existing = {
        (h.branch_id, h.calendar_year): h
        for h in db.execute(select(BranchHolidayYear)).scalars().all()
    }
    created = skipped = 0
    for r in read_csv(path):
        bname = (r.get("Customer Branch") or "").strip()
        year_raw = (r.get("Calendar Year") or "").strip()
        if not bname or not year_raw:
            continue
        try:
            year = int(float(year_raw))
        except ValueError:
            out.append(f"  ! holiday year skipped, bad year {year_raw!r}: {bname}")
            skipped += 1
            continue
        matches = by_name.get(norm(bname), [])
        if not matches:
            out.append(f"  ! holiday year skipped, unknown branch: {bname} ({year})")
            skipped += 1
            continue
        if len(matches) > 1:
            out.append(f"  ! holiday year skipped, ambiguous branch name: {bname} ({year})")
            skipped += 1
            continue
        branch = matches[0]
        is_freeze = norm(r.get("IsFreeze")) in ("true", "1", "yes", "y")
        row = existing.get((branch.id, year))
        if row is None:
            row = BranchHolidayYear(branch_id=branch.id, calendar_year=year,
                                    is_freeze=is_freeze)
            db.add(row)
            existing[(branch.id, year)] = row
            created += 1
            out.append(f"  + holiday year: {branch.branch_name} / {year}")
        else:
            row.is_freeze = is_freeze
            out.append(f"  ~ holiday year: {branch.branch_name} / {year} (already present)")
    return created, skipped


def main() -> int:
    argv = sys.argv[1:]
    apply = "--apply" in argv

    def path_for(flag: str, default: str) -> Path | None:
        wanted = any(a in argv for a in ("--contacts", "--roles", "--holidays"))
        if flag in argv:
            i = argv.index(flag)
            if i + 1 < len(argv) and not argv[i + 1].startswith("--"):
                return Path(argv[i + 1])
            return TEMPLATES / default
        return None if wanted else TEMPLATES / default

    p_contacts = path_for("--contacts", "customer_contacts.csv")
    p_roles = path_for("--roles", "contact_roles.csv")
    p_holidays = path_for("--holidays", "branch_holiday_years.csv")

    print(f"mode: {'APPLY' if apply else 'DRY RUN'}\n")
    db = get_session_factory()()
    out: list[str] = []
    try:
        if p_roles:
            if not p_roles.exists():
                print(f"roles CSV not found: {p_roles}")
                return 2
            c, u = import_roles(db, p_roles, out)
            print(f"ROLES: {c} created, {u} existing")
        if p_contacts:
            if not p_contacts.exists():
                print(f"contacts CSV not found: {p_contacts}")
                return 2
            c, u = import_contacts(db, p_contacts, out)
            print(f"CONTACTS: {c} created, {u} updated")
        if p_holidays:
            if not p_holidays.exists():
                print(f"holidays CSV not found: {p_holidays}")
                return 2
            c, s = import_holiday_years(db, p_holidays, out)
            print(f"HOLIDAY YEARS: {c} created, {s} skipped")
        print()
        for line in out:
            print(line)

        if apply:
            db.commit()
            print("\nAPPLIED - import complete.")
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
