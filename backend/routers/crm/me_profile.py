"""Self-service profile API for the logged-in user (own record only).

  GET    /api/me/profile          -> combined identity + profile + last login
  PATCH  /api/me/profile          -> update full_name + profile extension fields
  POST   /api/me/avatar           -> upload avatar (jpg/png/webp, <= 5 MB)
  DELETE /api/me/avatar           -> remove avatar (falls back to initials)
  POST   /api/me/change-password  -> verify current + set new (pbkdf2)

Every endpoint operates on the authenticated user (crm_deps.get_current_user);
there is no user_id in the path, so a user can only touch their OWN profile.
Legacy `registration_data` is updated with parameterised raw SQL (never altered
structurally); the extension fields live in the CRM `user_profiles` table.
"""
from __future__ import annotations


import sqlalchemy as sa
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from crm_deps import CurrentUser, get_crm_db, get_current_user
from models import UserProfile
from schemas.common import envelope
from schemas.me_profile import ChangePassword, ProfileUpdate
from services.crm_common import resolve_crm_file, save_upload

router = APIRouter(prefix="/api/me", tags=["CRM: My Profile"])

_AVATAR_TYPES = {"image/jpeg", "image/jpg", "image/png", "image/webp"}
_AVATAR_EXTS = (".jpg", ".jpeg", ".png", ".webp")
_MAX_AVATAR_BYTES = 5 * 1024 * 1024  # 5 MB


def _profile_row(db: Session, user_id: int) -> UserProfile | None:
    return db.execute(select(UserProfile).where(UserProfile.user_id == user_id)).scalar_one_or_none()


def _ensure_profile(db: Session, user_id: int) -> UserProfile:
    prof = _profile_row(db, user_id)
    if prof is None:
        prof = UserProfile(user_id=user_id)
        db.add(prof)
        db.flush()
    return prof


def _serialize(db: Session, user: CurrentUser) -> dict:
    row = db.execute(
        sa.text(
            "SELECT full_name, email, username, role, created_at_ist "
            "FROM registration_data WHERE id = :id"
        ),
        {"id": user.id},
    ).mappings().first()
    prof = _profile_row(db, user.id)
    last_login = db.execute(
        sa.text(
            "SELECT login_at_ist FROM login_data "
            "WHERE user_id = :id AND success = 1 ORDER BY id DESC LIMIT 1"
        ),
        {"id": user.id},
    ).scalar()
    return {
        "id": user.id,
        "username": row["username"] if row else user.username,
        "full_name": row["full_name"] if row else user.full_name,
        "email": row["email"] if row else user.email,
        "role": row["role"] if row else None,
        "roles": sorted(user.roles),
        "date_joined": row["created_at_ist"] if row else None,
        "last_login": last_login,
        "phone": prof.phone if prof else None,
        "job_title": prof.job_title if prof else None,
        "department": prof.department if prof else None,
        "timezone": prof.timezone if prof else None,
        "avatar_url": prof.avatar_url if prof else None,
    }


@router.get("/profile")
def get_profile(db: Session = Depends(get_crm_db), user: CurrentUser = Depends(get_current_user)):
    return envelope(_serialize(db, user), message="Profile fetched")


@router.patch("/profile")
def update_profile(
    payload: ProfileUpdate,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(get_current_user),
):
    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=400, detail="No fields to update")

    # full_name lives on the legacy users table.
    if "full_name" in changes and changes["full_name"] is not None:
        name = str(changes.pop("full_name")).strip()
        if not name:
            raise HTTPException(status_code=400, detail="Full name cannot be empty")
        db.execute(
            sa.text("UPDATE registration_data SET full_name = :n WHERE id = :id"),
            {"n": name, "id": user.id},
        )

    # Remaining fields are the profile extension.
    if changes:
        prof = _ensure_profile(db, user.id)
        for field in ("phone", "job_title", "department", "timezone"):
            if field in changes:
                val = changes[field]
                setattr(prof, field, (str(val).strip() or None) if val is not None else None)

    db.commit()
    return envelope(_serialize(db, user), message="Profile updated")


@router.post("/avatar")
def upload_avatar(
    file: UploadFile = File(...),
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(get_current_user),
):
    filename = (file.filename or "").lower()
    ctype = (file.content_type or "").lower()
    # BOTH must pass. With `and` (the old test) it was enough to satisfy either,
    # so `Content-Type: image/png` + `filename="x.html"` slipped through and was
    # stored as .html — then served inline from the app origin as stored XSS.
    if ctype not in _AVATAR_TYPES or not any(filename.endswith(e) for e in _AVATAR_EXTS):
        raise HTTPException(status_code=400, detail="Avatar must be a JPG, PNG or WEBP image")
    data = file.file.read()
    # Trust the bytes, not the headers: both of the above are attacker-controlled.
    if not _looks_like_image(data):
        raise HTTPException(status_code=400, detail="That file is not a valid image")
    if len(data) > _MAX_AVATAR_BYTES:
        raise HTTPException(status_code=400, detail="Image is larger than 5 MB")
    if not data:
        raise HTTPException(status_code=400, detail="Empty file")
    file.file.seek(0)
    raw = save_upload(file, "avatars")  # -> /api/crm-files/avatars/<uuid> (auth-gated)
    rel = raw.split("/api/crm-files/", 1)[-1]  # avatars/<uuid>
    # Avatars are served through a PUBLIC, avatars-only route so plain <img src>
    # works everywhere without a bearer token.
    public_url = f"/api/me/avatar-file/{rel}"
    prof = _ensure_profile(db, user.id)
    prof.avatar_url = public_url
    db.commit()
    return envelope({"avatar_url": public_url}, message="Avatar updated")



#: Extension -> the media type we will serve it as. Anything outside this map is
#: refused rather than guessed.
_AVATAR_MEDIA_BY_EXT = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".png": "image/png", ".webp": "image/webp",
}

#: Magic bytes for the formats we accept.
_IMAGE_SIGNATURES = (
    b"\xff\xd8\xff",       # jpeg
    b"\x89PNG\r\n\x1a\n",  # png
)


def _looks_like_image(data: bytes) -> bool:
    if data.startswith(_IMAGE_SIGNATURES):
        return True
    # webp: "RIFF....WEBP"
    return len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP"

@router.get("/avatar-file/{rel_path:path}")
def serve_avatar(rel_path: str):
    """Public avatar file server (no auth): restricted to the avatars/ subdir with
    random UUID filenames, so it can be used directly as an <img> source."""
    if not rel_path.startswith("avatars/") or ".." in rel_path:
        raise HTTPException(status_code=404, detail="Not found")
    path = resolve_crm_file(rel_path)
    if path is None:
        raise HTTPException(status_code=404, detail="File not found")
    # Pin the type from our own allowlist. Letting Starlette guess meant an
    # .html upload was returned as text/html and executed on this origin.
    media_type = _AVATAR_MEDIA_BY_EXT.get(path.suffix.lower())
    if media_type is None:
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(
        path,
        media_type=media_type,
        headers={"X-Content-Type-Options": "nosniff",
                 "Content-Security-Policy": "default-src 'none'; img-src 'self'"},
    )


@router.delete("/avatar")
def delete_avatar(db: Session = Depends(get_crm_db), user: CurrentUser = Depends(get_current_user)):
    prof = _profile_row(db, user.id)
    if prof is not None:
        prof.avatar_url = None
        db.commit()
    return envelope({"avatar_url": None}, message="Avatar removed")


@router.post("/change-password")
def change_password(
    payload: ChangePassword,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(get_current_user),
):
    import password_hashing as pwh

    row = db.execute(
        sa.text("SELECT password_hash, password_salt FROM registration_data WHERE id = :id"),
        {"id": user.id},
    ).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="User not found")

    # Constant-time verify of the current password (supports legacy + modern rows).
    ok, _needs = pwh.verify_password(payload.current_password, row["password_hash"], row["password_salt"])
    if not ok:
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    if payload.new_password == payload.current_password:
        raise HTTPException(status_code=400, detail="New password must differ from the current one")
    try:
        pwh.validate_password(payload.new_password)
    except pwh.PasswordPolicyError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    new_hash = pwh.hash_password(payload.new_password)  # modern bcrypt; salt column unused
    db.execute(
        sa.text("UPDATE registration_data SET password_hash = :h, password_salt = :s WHERE id = :id"),
        {"h": new_hash, "s": "", "id": user.id},
    )
    db.commit()
    return envelope({"changed": True}, message="Password changed")
