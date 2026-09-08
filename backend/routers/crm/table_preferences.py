"""Per-user list layout: which columns, in what order, and the sort priority.

One row per (user, table). The layout is stored as JSON so adding another
customisable list needs no migration — only a `table_key` and a set of allowed
column keys registered below.

Validation matters here: a stale or hand-edited config must never be able to
break the page, so unknown column keys are dropped on both write and read, and
anything missing is appended with the default visibility.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from crm_deps import CurrentUser, any_crm_role, get_crm_db
from models import UserTablePreference
from schemas.common import envelope

router = APIRouter(prefix="/api/me/table-preferences", tags=["CRM: Table Preferences"])


class ColumnPref(BaseModel):
    key: str
    visible: bool = True


class SortPref(BaseModel):
    by: str
    dir: str = "asc"


class TablePreferenceIn(BaseModel):
    columns: list[ColumnPref] = Field(default_factory=list)
    #: Ordered: the first entry wins, later entries break ties (Excel-style).
    sort: list[SortPref] = Field(default_factory=list)


#: table_key -> (column keys that may be shown, keys that may be sorted on).
#: Sortable is a subset: computed columns (the CTC slab budget, the latest
#: interview) are rendered per row and have no SQL column to order by.
TABLE_REGISTRY: dict[str, dict] = {
    "candidate_profiles": {
        "columns": [
            "candidate_name", "email", "phone", "experience_years", "notice_period",
            "technical_domain", "opportunity", "customer", "pipeline_status", "stage",
            "ai_interview",
            "current_ctc", "expected_ctc", "hike_percent", "approved_ctc_budget",
            "interview_round", "interview_status",
            "interview_datetime", "resume_url", "resignation_certificate_url",
            "commercial_approval_status", "customer_submission_date",
            "customer_onboarding_date", "karnex_onboarding_date",
            "ta_owner_name", "applied_on", "created_at",
        ],
        "sortable": [
            "candidate_name", "email", "experience_years", "notice_period",
            # The directory sorts by AI score by default — see _LATEST_AI_SCORE
            # in routers/crm/candidate_profiles.py for how a per-session score
            # becomes a sortable per-profile value.
            "ai_interview",
            "opportunity", "customer",
            "pipeline_status", "current_ctc", "expected_ctc", "hike_percent",
            "customer_submission_date", "customer_onboarding_date",
            "karnex_onboarding_date",
            "ta_owner_name", "applied_on", "created_at",
        ],
    },
}
#: How many sort levels a user may stack. Beyond this the query stops being
#: meaningful and starts being a way to make the database work hard for nothing.
MAX_SORT_LEVELS = 4


def _registry_or_404(table_key: str) -> dict:
    reg = TABLE_REGISTRY.get(table_key)
    if reg is None:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown table '{table_key}'. Known: {', '.join(sorted(TABLE_REGISTRY))}",
        )
    return reg


def _clean(config: dict, reg: dict) -> dict:
    """Drop unknown keys, append missing ones, cap the sort depth.

    Called on read as well as write: a column removed from the app in a later
    release must not leave someone with a broken saved layout.
    """
    allowed = reg["columns"]
    sortable = set(reg["sortable"])
    seen: set[str] = set()
    columns = []
    for item in config.get("columns") or []:
        key = (item or {}).get("key")
        if key in allowed and key not in seen:
            seen.add(key)
            columns.append({"key": key, "visible": bool(item.get("visible", True))})
    # Anything the saved layout has not seen yet goes to the end, hidden, so a
    # newly added column never rearranges a layout someone already tuned.
    for key in allowed:
        if key not in seen:
            columns.append({"key": key, "visible": False})

    sort = []
    for item in (config.get("sort") or [])[:MAX_SORT_LEVELS]:
        by = (item or {}).get("by")
        if by in sortable and by not in {s["by"] for s in sort}:
            direction = str(item.get("dir", "asc")).lower()
            sort.append({"by": by, "dir": "desc" if direction == "desc" else "asc"})
    return {"columns": columns, "sort": sort}


@router.get("/{table_key}")
def get_preference(table_key: str,
                   db: Session = Depends(get_crm_db),
                   user: CurrentUser = Depends(any_crm_role)):
    reg = _registry_or_404(table_key)
    row = db.execute(
        select(UserTablePreference).where(
            UserTablePreference.user_id == user.id,
            UserTablePreference.table_key == table_key,
        )
    ).scalars().first()
    config = _clean(row.config or {} if row else {}, reg)
    return envelope(data={
        "table_key": table_key,
        "columns": config["columns"],
        "sort": config["sort"],
        "available_columns": reg["columns"],
        "sortable_columns": reg["sortable"],
        "max_sort_levels": MAX_SORT_LEVELS,
        "is_customised": row is not None,
    })


@router.put("/{table_key}")
def save_preference(table_key: str, payload: TablePreferenceIn,
                    db: Session = Depends(get_crm_db),
                    user: CurrentUser = Depends(any_crm_role)):
    reg = _registry_or_404(table_key)
    config = _clean(payload.model_dump(), reg)
    if not any(c["visible"] for c in config["columns"]):
        raise HTTPException(status_code=400, detail="Keep at least one column visible")

    row = db.execute(
        select(UserTablePreference).where(
            UserTablePreference.user_id == user.id,
            UserTablePreference.table_key == table_key,
        )
    ).scalars().first()
    if row is None:
        row = UserTablePreference(user_id=user.id, table_key=table_key, config=config)
        db.add(row)
    else:
        row.config = config
    db.commit()
    return envelope(data={"table_key": table_key, **config}, message="Layout saved")


@router.delete("/{table_key}")
def reset_preference(table_key: str,
                     db: Session = Depends(get_crm_db),
                     user: CurrentUser = Depends(any_crm_role)):
    """Back to the default layout for this user."""
    _registry_or_404(table_key)
    row = db.execute(
        select(UserTablePreference).where(
            UserTablePreference.user_id == user.id,
            UserTablePreference.table_key == table_key,
        )
    ).scalars().first()
    if row is not None:
        db.delete(row)
        db.commit()
    return envelope(data={"table_key": table_key}, message="Layout reset to default")
