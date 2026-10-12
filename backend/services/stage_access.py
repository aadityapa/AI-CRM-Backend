"""Which candidate STAGES each role sees (13 Oct 2026, migration 0133).

User ask: "a role-wise filter — the Sales team only needs the candidates from
Sales Screening on; Admin manages it from Access Control, for Candidate
Profiles and inside every Opportunity." The stages are the phases the Stage
column and the stage chips speak (`services/candidate_status.STAGES`:
Sourcing … Joined, Closed).

Rules (user decisions, 13 Oct 2026):
  * Set PER ROLE on Access Control ▸ Roles — built-in roles in
    `roles.stage_access`, custom roles in `custom_roles.stage_access`.
    NULL = the role's default from code (`default_stages`).
  * Defaults: Sales, Sales Head and any custom role that stands for them (the
    Sales Manager — `role_implications.approval_default_roles`) see from Sales
    Screening onward, plus Joined and Closed; every other role sees all.
  * A login with several roles sees the UNION of its roles' stages. Admin and
    CEO always see everything (not configurable).
  * A stage a role does not have hides its chip AND its candidates from the
    lists, counts and exports (`candidate_status.phase_access_clause`). A
    direct link to one profile still opens.
"""
from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from services.candidate_status import STAGE_LABEL, STAGES

#: Every stage key, in flow order.
ALL_STAGES: list[str] = [k for k, _ in STAGES]
#: Where the Sales family's default view starts.
SALES_FROM = "sales_screening"
SALES_FAMILY_STAGES: list[str] = ALL_STAGES[ALL_STAGES.index(SALES_FROM):]
#: Built-in roles whose default is the Sales view.
SALES_FAMILY: frozenset[str] = frozenset({"Sales", "Sales_Head"})
#: Always see everything — not offered on the Roles page.
UNRESTRICTED: frozenset[str] = frozenset({"Admin", "CEO"})

#: One-click choices the Roles page offers.
PRESETS: list[dict] = [
    {"key": "all", "label": "Every stage", "stages": ALL_STAGES},
    {"key": "sales", "label": "From Sales Screening", "stages": SALES_FAMILY_STAGES},
    {"key": "recruitment", "label": "Up to Technical Interview + Closed",
     "stages": ["sourcing", "technical_screening", "technical_interview", "closed"]},
    {"key": "hr", "label": "From Customer Shortlisted",
     "stages": ALL_STAGES[ALL_STAGES.index("selection"):]},
]


def clean_stages(raw: Iterable[str] | None) -> list[str]:
    """Validated stage keys in flow order (ValueError on an unknown key or an
    empty list — a role that sees no stage would show an empty page)."""
    keys = {str(k).strip() for k in (raw or []) if str(k).strip()}
    unknown = sorted(k for k in keys if k not in STAGE_LABEL)
    if unknown:
        raise ValueError(f"Unknown stage {', '.join(repr(k) for k in unknown)}")
    if not keys:
        raise ValueError("Pick at least one stage")
    return [k for k in ALL_STAGES if k in keys]


def _builtin_names() -> set[str]:
    from models.rbac import RoleName
    return {r.value for r in RoleName}


def default_stages(name: str) -> list[str]:
    """The stages a role sees when Admin never set them. PURE."""
    from services.role_implications import approval_default_roles

    if name in UNRESTRICTED:
        return list(ALL_STAGES)
    if name in SALES_FAMILY or approval_default_roles({name}) & SALES_FAMILY:
        return list(SALES_FAMILY_STAGES)
    return list(ALL_STAGES)


def effective_stages(name: str, saved) -> list[str]:
    """`saved` (a stored list, or None) → the stages the role sees. PURE."""
    if name in UNRESTRICTED:
        return list(ALL_STAGES)
    if saved is None:
        return default_stages(name)
    try:
        return clean_stages(saved)
    except ValueError:
        return default_stages(name)   # a corrupt row never empties a page


def role_payload(name: str, saved) -> dict:
    """What the Roles page prints for one role."""
    stages = effective_stages(name, saved)
    return {
        "stages": stages,
        "all": len(stages) == len(ALL_STAGES),
        "is_default": saved is None,
        "default": default_stages(name),
        "configurable": name not in UNRESTRICTED,
    }


def _saved_builtin(db: Session) -> dict[str, list | None]:
    """{built-in role name: stored list | None}. Never raises (a database
    before migration 0133 reads as "all defaults")."""
    from models.rbac import Role

    try:
        with db.begin_nested():
            rows = db.execute(select(Role.name, Role.stage_access)).all()
    except Exception:
        return {}
    return {(n.value if hasattr(n, "value") else str(n)): s for n, s in rows}


def _saved_custom(db: Session, names: Iterable[str]) -> dict[str, list | None]:
    """{custom role name (lower-case): stored list | None} for these names."""
    from models.custom_roles import CustomRole

    wanted = {str(n).strip().lower() for n in names if str(n).strip()}
    if not wanted:
        return {}
    try:
        with db.begin_nested():
            rows = db.execute(select(CustomRole.name, CustomRole.stage_access)).all()
    except Exception:
        return {}
    return {str(n).strip().lower(): s for n, s in rows if str(n).strip().lower() in wanted}


def stages_for_roles(db: Session, roles: Iterable[str]) -> set[str] | None:
    """The union of these roles' stages; None = every stage (nothing to hide)."""
    names = {str(r) for r in roles or () if str(r).strip()}
    if not names or names & UNRESTRICTED:
        return None
    builtin = _builtin_names()
    saved_b = _saved_builtin(db) if names & builtin else {}
    saved_c = _saved_custom(db, names - builtin)
    union: set[str] = set()
    for name in names:
        saved = saved_b.get(name) if name in builtin else saved_c.get(name.lower())
        union |= set(effective_stages(name, saved))
    return None if union >= set(ALL_STAGES) else union


def allowed_stages(db: Session, user) -> set[str] | None:
    """The stages this login sees; None = all. Uses the roles the user HOLDS
    (a Sales Manager is judged by the Sales Manager role, not the Sales role it
    implies for role checks)."""
    if getattr(user, "is_admin", False):
        return None
    held = set(getattr(user, "held_roles", None) or ()) or set(getattr(user, "roles", None) or ())
    return stages_for_roles(db, held)


def set_role_stages(db: Session, *, builtin: str | None = None, custom_id: int | None = None,
                    stages: Iterable[str] | None) -> tuple[str, list | None, list | None]:
    """Save one role's stages (`stages=None` = back to the default). Returns
    (role name, before, after) for the audit log. Caller commits."""
    from fastapi import HTTPException
    from models.custom_roles import CustomRole
    from models.rbac import Role, RoleName

    try:
        clean = None if stages is None else clean_stages(stages)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if builtin is not None:
        if builtin in UNRESTRICTED:
            raise HTTPException(status_code=400, detail=f"{builtin} always sees every stage")
        try:
            enum = RoleName(builtin)
        except ValueError:
            raise HTTPException(status_code=404, detail="Role not found")
        row = db.execute(select(Role).where(Role.name == enum)).scalar_one_or_none()
        if row is None:
            row = Role(name=enum)
            db.add(row)
        before = row.stage_access
        row.stage_access = clean
        return builtin, before, clean
    role = db.get(CustomRole, custom_id) if custom_id is not None else None
    if role is None:
        raise HTTPException(status_code=404, detail="Role not found")
    before = role.stage_access
    role.stage_access = clean
    return role.name, before, clean


def catalogue() -> dict:
    """Stages + presets for the Roles page."""
    return {"stages": [{"key": k, "label": label} for k, label in STAGES], "presets": PRESETS}


def describe(stages: list[str] | None) -> str:
    """"Sales Screening → Closed (9 of 12)" for audit lines. PURE."""
    if stages is None:
        return "role default"
    if len(stages) == len(ALL_STAGES):
        return "every stage"
    return f"{', '.join(STAGE_LABEL[s] for s in stages)} ({len(stages)} of {len(ALL_STAGES)})"
