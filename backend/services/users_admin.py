"""Admin user-management services.

User records live in the legacy registration_data table (owned by auth_db.py,
raw SQL). We REUSE auth_db.register_user — which applies the platform's
PBKDF2-HMAC-SHA256 password hashing (auth_db._hash_password) and IST
timestamps — instead of reimplementing any of it. CRM roles live in the
user_roles/roles tables (SQLAlchemy models).
"""
from __future__ import annotations

import json

import sqlalchemy as sa
from fastapi import HTTPException
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from auth_db import register_user
from crm_db import CrmNotConfiguredError, crm_database_url
from crm_deps import PageParams
from models import Employee, Role, RoleName, UserRole
from models.user_profiles import UserProfile

VALID_ROLE_NAMES = [m.value for m in RoleName]


def _legacy_db_target() -> str:
    """psycopg2 DSN of the database holding registration_data.

    The CRM session reads registration_data through the same Postgres the CRM
    is configured for, so that DSN (minus the SQLAlchemy driver suffix) is the
    correct target for auth_db's raw-SQL helpers.
    """
    try:
        url = crm_database_url()
    except CrmNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    return url.replace("postgresql+psycopg2://", "postgresql://", 1)


def _role_member(name: str) -> RoleName:
    for member in RoleName:
        if member.value == name:
            return member
    raise HTTPException(
        status_code=400,
        detail=f"Unknown CRM role '{name}'. Valid roles: {', '.join(VALID_ROLE_NAMES)}",
    )


def _auto_assign_role_template(db: Session, user_id: int, members: list[RoleName]) -> None:
    """Give an untemplated user the ACTIVE template tagged with their role.

    Makes AccessTemplate.role mean something: an admin who builds a template
    "for RMG" gets it applied to new/re-roled RMG users automatically, in role
    order. Explicit assignment always wins — a user who already has a template
    (or a per-user tab override) is left completely alone. Best-effort: any
    failure must never block user creation or a role change.
    """
    try:
        from models.access_templates import AccessTemplate

        profile = db.execute(
            select(UserProfile).where(UserProfile.user_id == user_id)
        ).scalars().first()
        if profile is not None and (profile.access_template_id or profile.tab_access):
            return  # explicitly configured — never override
        for member in members:
            t = db.execute(
                select(AccessTemplate).where(
                    AccessTemplate.role == member.value,
                    AccessTemplate.is_active.is_(True),
                ).order_by(AccessTemplate.id)
            ).scalars().first()
            if t is None:
                continue
            if profile is None:
                db.add(UserProfile(user_id=user_id, access_template_id=t.id))
            else:
                profile.access_template_id = t.id
            return
    except Exception:  # noqa: BLE001 — auto-default must never break user admin
        return


def _get_or_create_role(db: Session, member: RoleName) -> Role:
    role = db.execute(select(Role).where(Role.name == member)).scalar_one_or_none()
    if role is None:
        role = Role(name=member)
        db.add(role)
        db.flush()
    return role


def _roles_map(db: Session, user_ids: list[int]) -> dict[int, list[str]]:
    out: dict[int, list[str]] = {}
    if not user_ids:
        return out
    rows = db.execute(
        select(UserRole.user_id, Role.name)
        .join(Role, Role.id == UserRole.role_id)
        .where(UserRole.user_id.in_(user_ids))
    ).all()
    for uid, rname in rows:
        out.setdefault(uid, []).append(rname.value if hasattr(rname, "value") else str(rname))
    for uid in out:
        out[uid].sort()
    return out


def _require_user_row(db: Session, user_id: int) -> dict:
    row = db.execute(
        sa.text(
            "SELECT id, full_name, email, username, role, "
            "COALESCE(is_active, TRUE) AS is_active "
            "FROM registration_data WHERE id = :i"
        ),
        {"i": user_id},
    ).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="User not found")
    return dict(row)


def _user_out(row: dict, roles: list[str], tab_access: list[str] | None = None,
              access_template_id: int | None = None) -> dict:
    return {
        "id": row["id"],
        "full_name": row["full_name"] or "",
        "email": row["email"] or "",
        "username": row["username"] or "",
        "legacy_role": row["role"] or "",
        "is_active": bool(row["is_active"]),
        "roles": roles,
        # None => no override (full role-based access); list => allowed tab keys.
        "tab_access": tab_access,
        "access_template_id": access_template_id,
    }


def _decode_tab_access(raw: str | None) -> list[str] | None:
    if not raw:
        return None
    try:
        val = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if isinstance(val, list):
        return [str(x) for x in val]
    return None


def _template_map(db: Session, user_ids: list[int]) -> dict[int, int | None]:
    out: dict[int, int | None] = {}
    if not user_ids:
        return out
    rows = db.execute(
        select(UserProfile.user_id, UserProfile.access_template_id).where(UserProfile.user_id.in_(user_ids))
    ).all()
    for uid, tid in rows:
        out[uid] = tid
    return out


def _tab_access_map(db: Session, user_ids: list[int]) -> dict[int, list[str] | None]:
    out: dict[int, list[str] | None] = {}
    if not user_ids:
        return out
    rows = db.execute(
        select(UserProfile.user_id, UserProfile.tab_access).where(UserProfile.user_id.in_(user_ids))
    ).all()
    for uid, raw in rows:
        out[uid] = _decode_tab_access(raw)
    return out


def get_tab_access(db: Session, user_id: int) -> list[str] | None:
    _require_user_row(db, user_id)
    raw = db.execute(
        select(UserProfile.tab_access).where(UserProfile.user_id == user_id)
    ).scalar_one_or_none()
    return _decode_tab_access(raw)


def get_field_access(db: Session, user_id: int) -> dict | None:
    raw = db.execute(
        select(UserProfile.field_access).where(UserProfile.user_id == user_id)
    ).scalar_one_or_none()
    if not raw:
        return None
    try:
        val = json.loads(raw)
        return val if isinstance(val, dict) else None
    except (ValueError, TypeError):
        return None


def set_tab_access(db: Session, user_id: int, tabs: list[str] | None,
                   field_access: dict | None = None) -> dict:
    row = _require_user_row(db, user_id)
    # Normalise: None (or empty -> None) clears the override; else store a JSON array.
    encoded = None
    if tabs:
        clean = sorted({str(t).strip() for t in tabs if str(t).strip()})
        encoded = json.dumps(clean) if clean else None
    # field_access: keep only non-empty per-tab lists; empty -> no restriction (None).
    fa_encoded = None
    if field_access:
        cleaned = {
            str(k): sorted({str(x) for x in v})
            for k, v in field_access.items()
            if isinstance(v, list) and v
        }
        fa_encoded = json.dumps(cleaned) if cleaned else None
    profile = db.execute(
        select(UserProfile).where(UserProfile.user_id == user_id)
    ).scalar_one_or_none()
    if profile is None:
        profile = UserProfile(user_id=user_id, tab_access=encoded, field_access=fa_encoded)
        db.add(profile)
    else:
        profile.tab_access = encoded
        profile.field_access = fa_encoded
    db.commit()
    roles = _roles_map(db, [user_id]).get(user_id, [])
    return _user_out(row, roles, _decode_tab_access(encoded))


def list_users(db: Session, p: PageParams) -> tuple[list[dict], dict]:
    # Users tab manages application login accounts only (legacy role = hr).
    # Candidate interview accounts stay out of Admin/CEO user management.
    clauses = ["LOWER(role) = 'hr'"]
    params: dict = {}
    if p.search:
        clauses.append("(LOWER(username) LIKE :like OR LOWER(email) LIKE :like)")
        params["like"] = f"%{p.search.lower()}%"
    where = "WHERE " + " AND ".join(clauses)
    total = db.execute(
        sa.text(f"SELECT COUNT(*) FROM registration_data {where}"), params
    ).scalar() or 0
    rows = db.execute(
        sa.text(
            "SELECT id, full_name, email, username, role, "
            "COALESCE(is_active, TRUE) AS is_active "
            f"FROM registration_data {where} ORDER BY id ASC "
            "LIMIT :limit OFFSET :offset"
        ),
        {**params, "limit": p.limit, "offset": p.offset},
    ).mappings().all()
    ids = [r["id"] for r in rows]
    roles = _roles_map(db, ids)
    tabs = _tab_access_map(db, ids)
    templates = _template_map(db, ids)
    users = [_user_out(dict(r), roles.get(r["id"], []), tabs.get(r["id"]), templates.get(r["id"]))
             for r in rows]
    pages = (total + p.limit - 1) // p.limit if p.limit else 1
    meta = {"page": p.page, "limit": p.limit, "total": total, "pages": pages}
    return users, meta


def create_user(db: Session, full_name: str, email: str, username: str,
                password: str, legacy_role: str, role_names: list[str]) -> dict:
    # Validate CRM roles BEFORE touching the legacy table.
    members = [_role_member(n) for n in dict.fromkeys(role_names or [])]
    if not members:
        raise HTTPException(
            status_code=400,
            detail="Assign at least one CRM role so the user can access the application.",
        )
    try:
        created = register_user(
            _legacy_db_target(),
            full_name=full_name, email=email, username=username,
            password=password, role=legacy_role or "hr",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    user_id = int(created["id"])
    for member in members:
        role = _get_or_create_role(db, member)
        db.add(UserRole(user_id=user_id, role_id=role.id))
    _auto_assign_role_template(db, user_id, members)
    db.commit()
    row = _require_user_row(db, user_id)
    return _user_out(row, sorted(m.value for m in members))


def replace_roles(db: Session, user_id: int, role_names: list[str]) -> dict:
    row = _require_user_row(db, user_id)
    members = [_role_member(n) for n in dict.fromkeys(role_names or [])]
    db.execute(delete(UserRole).where(UserRole.user_id == user_id))
    for member in members:
        role = _get_or_create_role(db, member)
        db.add(UserRole(user_id=user_id, role_id=role.id))
    _auto_assign_role_template(db, user_id, members)
    db.commit()
    return _user_out(row, sorted(m.value for m in members))


def set_user_active(db: Session, user_id: int, active: bool) -> dict:
    _require_user_row(db, user_id)
    db.execute(
        sa.text("UPDATE registration_data SET is_active = :a WHERE id = :i"),
        {"a": active, "i": user_id},
    )
    db.commit()
    row = _require_user_row(db, user_id)
    return _user_out(row, _roles_map(db, [user_id]).get(user_id, []))


def _table_has_column(db: Session, table: str, column: str) -> bool:
    return bool(
        db.execute(
            sa.text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name = :t AND column_name = :c"
            ),
            {"t": table, "c": column},
        ).scalar()
    )


def _detach_user_refs(db: Session, user_id: int, reassign_to: int) -> None:
    """Clear/reassign FK refs so Admin/CEO can hard-delete login accounts."""
    nullable = [
        ("candidate_profiles", "ta_owner_id"),
        ("timesheets", "approved_by"),
        ("timesheet_uploads", "uploaded_by"),
        ("interview_events", "created_by"),
        ("leave_applications", "decided_by"),
        ("requirements", "sales_head_approved_by"),
        ("requirements", "engineering_reviewed_by"),
        ("requirement_documents", "uploaded_by"),
        ("resumes", "screened_by"),
        ("ai_interview_links", "scheduled_by"),
        ("invoices", "approved_by"),
        ("opportunity_documents", "uploaded_by"),
        ("opportunities", "sales_head_approved_by"),
        ("template_requests", "fulfilled_by"),
        ("template_requests", "prepared_by"),
        ("employees", "user_id"),
    ]
    owned = [
        ("user_roles", "user_id"),
        ("user_profiles", "user_id"),
        ("notifications", "user_id"),
        ("user_table_preferences", "user_id"),
        ("timesheet_drafts", "user_id"),
        ("requirement_watchers", "user_id"),
        ("opportunity_watchers", "user_id"),
        ("invoice_watchers", "user_id"),
        ("login_history", "user_id"),
    ]
    reassign = [
        ("requirements", "created_by"),
        ("requirement_comments", "posted_by"),
        ("opportunities", "created_by"),
        ("invoices", "created_by"),
        ("template_requests", "requested_by"),
    ]
    for table, col in nullable:
        if _table_has_column(db, table, col):
            db.execute(
                sa.text(f"UPDATE {table} SET {col} = NULL WHERE {col} = :i"),
                {"i": user_id},
            )
    for table, col in owned:
        if _table_has_column(db, table, col):
            db.execute(sa.text(f"DELETE FROM {table} WHERE {col} = :i"), {"i": user_id})
    for table, col in reassign:
        if _table_has_column(db, table, col):
            db.execute(
                sa.text(f"UPDATE {table} SET {col} = :new WHERE {col} = :old"),
                {"new": reassign_to, "old": user_id},
            )


def delete_user(db: Session, user_id: int, actor_id: int) -> dict:
    """Hard-delete a login account. Admin/CEO: detach/reassign FKs first."""
    row = _require_user_row(db, user_id)
    if user_id == actor_id:
        raise HTTPException(status_code=400, detail="You cannot delete your own account.")
    _detach_user_refs(db, user_id, reassign_to=actor_id)
    try:
        db.execute(sa.text("DELETE FROM registration_data WHERE id = :i"), {"i": user_id})
        db.commit()
    except sa.exc.IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail=(
                "This user is still referenced by other records and cannot be deleted. "
                "Deactivate the user instead — deactivated accounts cannot log in."
            ),
        )
    return {"id": user_id, "username": row["username"]}


def toggle_portal_access(db: Session, user_id: int) -> dict:
    _require_user_row(db, user_id)
    employee = db.execute(
        select(Employee).where(Employee.user_id == user_id)
    ).scalar_one_or_none()
    if employee is None:
        raise HTTPException(status_code=404, detail="No employee is linked to this user")
    employee.portal_access = not bool(employee.portal_access)
    db.commit()
    return {
        "user_id": user_id,
        "employee_id": employee.id,
        "portal_access": bool(employee.portal_access),
    }
