"""Question Bank HTTP endpoints (interview platform).

Implements the API the admin dashboard already expects (src/api/questionBank.ts)
plus the template-wizard preview at /job/template/question-bank/preview. Auth uses
the legacy interview JWT (role 'hr'); the QB admin page is further gated to
super-admins in the UI.
"""
from __future__ import annotations


import jwt
from fastapi import APIRouter, Body, File, HTTPException, Request, UploadFile
from fastapi.responses import PlainTextResponse

import question_bank as qb
from auth_secret import auth_secret as _shared_auth_secret

router = APIRouter(tags=["Question Bank"])


def _auth_secret() -> str:
    """Delegates to auth_secret.py — no local fallback (see that module).

    A default here meant tokens could be forged by anyone reading the source.
    """
    return _shared_auth_secret()


def _require_hr(request: Request) -> dict:
    auth = (request.headers.get("Authorization") or "").strip()
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Authentication required")
    try:
        payload = jwt.decode(
            auth[len("Bearer "):].strip(), _auth_secret(),
            algorithms=["HS256"], options={"verify_signature": True, "verify_exp": True, "require": ["exp"]},
        )
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    if str(payload.get("role", "")).lower() != "hr":
        raise HTTPException(status_code=403, detail="Forbidden for this role")
    return payload


def _truthy(v: object) -> bool:
    return str(v).strip().lower() in {"1", "true", "yes", "on"}


def _csv_list(v: str) -> list[str]:
    return [x.strip() for x in (v or "").split(",") if x.strip()]


# ----------------------------------------------------------- QB admin API
@router.get("/api/question-bank/dashboard")
def qb_dashboard(request: Request):
    _require_hr(request)
    return qb.dashboard()


@router.get("/api/question-bank/questions")
def qb_list(
    request: Request, page: int = 1, pageSize: int = 25, role: str = "", skill: str = "",
    difficulty: str = "", category: str = "", search: str = "", isActive: str = "",
    approvalStatus: str = "",
):
    _require_hr(request)
    return qb.list_questions(page, pageSize, role, skill, difficulty, category, search, isActive, approvalStatus)


@router.get("/api/question-bank/roles")
def qb_roles(request: Request):
    _require_hr(request)
    return {"roles": qb.roles()}


@router.get("/api/question-bank/skills")
def qb_skills(request: Request, role: str = ""):
    _require_hr(request)
    return {"skills": qb.skills(role)}


@router.post("/api/question-bank/questions")
def qb_create(request: Request, payload: dict = Body(default_factory=dict)):
    _require_hr(request)
    try:
        return qb.create_question(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.put("/api/question-bank/questions/{qid}")
def qb_update(qid: str, request: Request, payload: dict = Body(default_factory=dict)):
    _require_hr(request)
    try:
        return qb.update_question(qid, payload)
    except KeyError:
        raise HTTPException(status_code=404, detail="Question not found")


@router.delete("/api/question-bank/questions/{qid}")
def qb_delete(qid: str, request: Request):
    _require_hr(request)
    try:
        qb.delete_question(qid)
        return {"status": "ok", "id": qid}
    except KeyError:
        raise HTTPException(status_code=404, detail="Question not found")


@router.patch("/api/question-bank/questions/{qid}/{action}")
def qb_toggle(qid: str, action: str, request: Request):
    _require_hr(request)
    if action not in ("activate", "deactivate"):
        raise HTTPException(status_code=400, detail="Invalid action")
    try:
        return qb.set_active(qid, action == "activate")
    except KeyError:
        raise HTTPException(status_code=404, detail="Question not found")


@router.post("/api/question-bank/import/csv")
async def qb_import(request: Request, file: UploadFile = File(...)):
    _require_hr(request)
    content = await file.read()
    return qb.import_csv(content)


@router.get("/api/question-bank/export")
def qb_export(request: Request):
    _require_hr(request)
    return PlainTextResponse(
        qb.export_csv(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=question_bank.csv"},
    )


@router.post("/api/question-bank/seed-sample")
def qb_seed(request: Request):
    _require_hr(request)
    return qb.seed_sample()


# ------------------------------------------------ template wizard preview
@router.post("/job/template/question-bank/preview")
async def qb_preview(request: Request):
    _require_hr(request)
    form = await request.form()
    return qb.preview_for_template(
        role=str(form.get("role", "")),
        required_skills=str(form.get("requiredSkills", "")),
        optional_skills=str(form.get("optionalSkills", "")),
        categories=_csv_list(str(form.get("categories", ""))),
        difficulties=_csv_list(str(form.get("difficulties", ""))),
        question_count=int(str(form.get("questionCount", "5") or "5")),
        randomization_enabled=_truthy(form.get("randomizationEnabled", "true")),
        avoid_duplicate_questions=_truthy(form.get("avoidDuplicateQuestions", "true")),
        excluded_ids=_csv_list(str(form.get("excludedQuestionIds", ""))),
    )
