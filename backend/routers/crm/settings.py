"""App settings endpoints (key/value pairs in app_settings, Admin-managed)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from crm_deps import CurrentUser, any_crm_role, get_crm_db, role_required
from models import AppSetting
from schemas.common import envelope
from schemas.masters import SettingOut, SettingValueIn
from services.crm_common import get_app_setting

router = APIRouter(prefix="/api", tags=["CRM: Settings"])

admin_only = role_required()

PASS_THRESHOLD_KEY = "ai_interview_pass_threshold"


@router.get("/settings")
def list_settings(db: Session = Depends(get_crm_db),
                  user: CurrentUser = Depends(admin_only)):
    rows = db.execute(select(AppSetting).order_by(AppSetting.key.asc())).scalars().all()
    return envelope(
        data=[SettingOut.model_validate(r).model_dump() for r in rows],
        message="App settings",
    )


@router.get("/settings/ai-interview-pass-threshold")
def get_ai_interview_pass_threshold(db: Session = Depends(get_crm_db),
                                    user: CurrentUser = Depends(any_crm_role)):
    value = get_app_setting(db, PASS_THRESHOLD_KEY, "60")
    return envelope(data={"key": PASS_THRESHOLD_KEY, "value": value},
                    message="AI interview pass threshold")


@router.get("/ui-text")
def ui_text(db: Session = Depends(get_crm_db),
            user: CurrentUser = Depends(any_crm_role)):
    """Admin-authored overrides for in-app teaching copy (`uitext.*` settings).

    Readable by every CRM user because the copy renders in everyone's UI;
    WRITES stay behind the generic admin-only PUT /settings/{key}. Keys:

      uitext.status.<StoredStatus>  — tooltip for that status badge
      uitext.empty.<page>           — body text of that page's empty state

    Empty values are dropped: "cleared" means "use the built-in copy",
    never "show nothing".
    """
    rows = db.execute(
        select(AppSetting).where(AppSetting.key.like("uitext.%"))
    ).scalars().all()
    return envelope(
        data={r.key: r.value for r in rows if (r.value or "").strip()},
        message="UI text overrides",
    )


@router.put("/settings/{key}")
def upsert_setting(key: str, payload: SettingValueIn,
                   db: Session = Depends(get_crm_db),
                   user: CurrentUser = Depends(admin_only)):
    key = (key or "").strip()
    if not key:
        raise HTTPException(status_code=400, detail="Setting key must not be empty")
    value = (payload.value or "").strip()

    if key == PASS_THRESHOLD_KEY:
        try:
            numeric = float(value)
        except ValueError:
            raise HTTPException(status_code=400,
                                detail=f"{PASS_THRESHOLD_KEY} must be numeric (0-100)")
        if not 0 <= numeric <= 100:
            raise HTTPException(status_code=400,
                                detail=f"{PASS_THRESHOLD_KEY} must be between 0 and 100")

    setting = db.get(AppSetting, key)
    if setting is None:
        setting = AppSetting(key=key, value=value, description=payload.description)
        db.add(setting)
        action = "created"
    else:
        setting.value = value
        if payload.description is not None:
            setting.description = payload.description
        action = "updated"
    db.commit()
    return envelope(data=SettingOut.model_validate(setting).model_dump(),
                    message=f"Setting '{key}' {action}")
