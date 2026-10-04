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
            # "stage" (the Zoho import's RMG/Sales/HR text) was removed on
            # 25 Sep 2026: the Status column now says where the candidate is.
            "technical_domain", "opportunity", "customer", "phase", "pipeline_status",
            "next_interview", "ai_interview", "ats_score",
            # One column per round (29 Sep 2026): verdict · date & time · panel ·
            # feedback — services/candidate_profiles.ROUND_COLUMNS.
            "round_tech_l1", "round_tech_l2", "round_tech_l3", "round_cust_l1", "round_cust_l2",
            "round_hr",
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
            "ai_interview", "ats_score",
            # Latest change first (29 Sep 2026) — the directory's default order.
            "last_activity",
            "opportunity", "customer",
            "pipeline_status", "current_ctc", "expected_ctc", "hike_percent",
            "customer_submission_date", "customer_onboarding_date",
            "karnex_onboarding_date",
            "ta_owner_name", "applied_on", "created_at",
        ],
        # Columns every user must SEE, even with a layout saved before they
        # existed: column -> the key it is placed after. Everything else new
        # arrives hidden at the end (see _clean). ATS Score (25 Sep 2026, user
        # request: "an ATS Score field where everyone can see").
        "announce": {"ats_score": "ai_interview",
                     # The redesigned directory (29 Sep 2026): the stage, the
                     # next interview and the round ladder arrive visible.
                     "phase": "customer", "next_interview": "pipeline_status",
                     "round_tech_l1": "ats_score", "round_tech_l2": "round_tech_l1",
                     "round_cust_l1": "round_tech_l2", "round_cust_l2": "round_cust_l1",
                     "round_hr": "round_cust_l2"},
    },
    # Requirement ▸ Applied Candidates (15 Sep 2026): the RMG asked for an
    # Excel-style column chooser so a wide list can be trimmed to what the
    # screening needs. The list is client-sorted, so nothing is sortable here;
    # the Actions column is always shown (the UI pins it).
    "requirement_resumes": {
        "columns": [
            "candidate_name", "applied_by",
            "profile_stage", "profile_pipeline_status", "availability", "received_date", "ats_score",
            "ai_interview_status", "rounds", "_actions",
        ],
        "sortable": [],
        # The separate "ATS Status" column was removed 28 Sep 2026 (user ask) —
        # the score ring carries the scan state, and saved layouts drop it
        # (`_clean`). "RMG Screening" and "Source" went the same way on 30 Sep
        # 2026. Stage stays beside Status (user decision, 30 Sep 2026) and is
        # announced so every saved layout shows it.
        # "availability" (notice period · last working day, 1 Oct 2026) is
        # announced after Status so every saved layout shows it.
        "announce": {"profile_stage": "applied_by", "availability": "profile_pipeline_status"},
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
    # An ANNOUNCED column the saved layout has never seen goes in visible,
    # right after its anchor — the product decided everyone sees it. Once the
    # user saves again it is part of their layout and theirs to hide.
    for key, after in (reg.get("announce") or {}).items():
        if key in allowed and key not in seen:
            seen.add(key)
            at = next((i + 1 for i, c in enumerate(columns) if c["key"] == after), len(columns))
            columns.insert(at, {"key": key, "visible": True})
    # Anything else the saved layout has not seen yet goes to the end, hidden,
    # so a newly added column never rearranges a layout someone already tuned.
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
