import copy
import os
from io import BytesIO
from pathlib import Path
from datetime import datetime, timezone, timedelta
from uuid import uuid4
from zipfile import ZipFile, BadZipFile
import xml.etree.ElementTree as ET
import json
import re
import time
import logging
import secrets
import hashlib
import hmac
import random
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from urllib.parse import urlparse
import ipaddress
import jwt
import shutil
import socket
import subprocess
import threading
from collections import OrderedDict, deque
from functools import lru_cache

from fastapi import BackgroundTasks, Body, FastAPI, File, Form, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from openai import OpenAIError
from pypdf import PdfReader
from starlette.middleware.gzip import GZipMiddleware

from ai import (
    align_qa_to_answered_turns,
    answer_turn_is_valid_for_scoring,
    answer_turn_was_attempted,
    is_time_limit_system_message,
    apply_decimal_scores_to_report,
    evaluate_turn_with_model,
    evaluate_with_model_skill_based,
    evaluate_fallback_skill_based,
    merge_per_question_eval_into_report,
    slice_qa_for_final_evaluation,
    extract_candidate_profile,
    extract_cv_skills,
    extract_jd_skills,
    extract_text_from_image_bytes,
    infer_interview_skills,
    merge_unique_skills,
    generate_followup_fallback,
    generate_followup_with_model,
    generate_questions_fallback,
    detect_skill_from_question,
    question_matches_skill,
    question_too_similar,
    synthesize_speech_bytes,
    transcribe_speech_bytes,
    evaluate_communication_skills,
)
from session import sessions, session_lock
from learning import (
    backfill_learning_from_records,
    append_from_evaluation,
    append_interview_turn,
    coach_hints_text,
    recently_asked_questions,
)
from excel_export import build_evaluation_xlsx
from config import (
    APP_TITLE,
    CORS_DEFAULT_ORIGINS,
    IMAGE_EXTENSIONS,
    OPENAI_CHAT_MODELS,
    REPORT_CODE,
    SESSION_ID,
    TEXT_EXTENSIONS,
)
from paths import (
    DATA_DIR,
    FRONTEND_DIR,
    HR_ACCESS_CODE_FILE,
    HR_RECORDS_FILE,
    KARNEX_DB_FILE,
    LEARNING_FILE,
    ensure_project_dirs,
    migrate_legacy_data_files,
)
from logging_setup import configure_logging
from auth_db import (
    bulk_import_interview_records,
    cascade_delete_candidate,
    create_interview_schedule,
    delete_interview_record,
    delete_interview_schedule,
    delete_interview_schedule_by_token,
    get_database_snapshot,
    get_interview_record_payload,
    get_interview_progress_by_invite,
    get_hr_candidate_decision,
    get_schedule_by_token,
    increment_schedule_login_attempts,
    init_auth_db,
    list_recoverable_interview_progress,
    list_hr_candidate_decisions,
    recent_questions_for_job_template,
    list_interview_records_for_candidate,
    list_interview_schedules,
    list_interview_integrity_logs,
    list_job_templates,
    get_job_template,
    search_candidate_suggestions,
    search_master_values,
    update_schedule_field,
    upsert_job_template,
    delete_job_template,
    register_user,
    get_user_by_email,
    create_password_reset,
    consume_password_reset,
    update_user_password,
    set_hr_candidate_decision,
    upsert_master_value,
    update_interview_hr_status,
    upsert_interview_record_snapshot,
    upsert_interview_progress,
    verify_login,
    _coerce_question_type,
    _normalized_manual_questions_for_job,
)
from hr.repository import (
    delete_record_by_id,
    delete_records_for_candidate,
    list_records_for_candidate,
    load_hr_records,
    upsert_hr_record_async,
)
from email_smtp import send_interview_invite_email, send_password_reset_email, smtp_configured
from hr.service import (
    build_hr_records_summary,
    build_report_record,
    find_hr_record,
)
from candidate.service import next_question_payload
from ats import AtsWeights, ats_score, ats_score_llm, list_job_configs
from services.interview.question_service import generate_mode_aware_questions
from utils.interview_limits import (
    MAX_COUNT_MODE_QUESTIONS,
    clamp_count_mode_questions,
    pool_questions_for_timing,
    resolve_template_num_q,
    trim_questions_for_count_mode,
)
from utils.interview_mode_mapper import normalize_interview_mode, to_display_label
from utils.question_uniqueness import (
    build_question_avoid_history,
    make_question_session_seed,
    prepare_unique_question_sequence,
    remember_asked_question,
)
from utils.auto_advance import parse_auto_advance_meta, record_auto_advance_turn_event, stamp_auto_advance_settings
from utils.speech_validation import (
    has_human_speech_evidence,
    skip_allowed_by_speech_evidence,
    skip_should_convert_to_answer,
)
from utils.template_settings import (
    communication_required,
    should_shuffle_manual_questions,
    stamp_template_settings,
)
from utils.time_warnings import AUDIT_FIELD_BY_KEY, stamp_time_warning_settings
from utils.strengths_weaknesses_analysis import attach_strengths_weaknesses_analysis
from utils.warmup import (
    filter_out_warmups,
    inject_warmup,
    is_warmup_index,
    stamp_introduction_question_types,
)
from prompt_logger import (
    init_prompt_log_table,
)
import response_cache
import password_hashing as pwh
import rate_limit as _rl
from openai_client import openai_key_configured
from auth_secret import auth_secret as _shared_auth_secret
from template_prompt import (
    build_default_template_prompt,
    build_template_prompt_context,
    estimate_tokens,
    render_prompt_preview,
    sanitize_prompt_input,
)


def _migrate_job_configs_to_db_if_needed() -> int:
    """
    One-time migration:
    - If DB has zero templates
    - And legacy file-based job configs exist (ats.py)
    Then import them into DB for future reference.
    """
    try:
        existing = list_job_templates(AUTH_DB_TARGET)
    except Exception:
        existing = []
    if existing:
        return 0
    legacy = []
    try:
        legacy = list_job_configs()
    except Exception:
        legacy = []
    imported = 0
    for j in legacy or []:
        try:
            upsert_job_template(AUTH_DB_TARGET, j)
            imported += 1
        except Exception:
            continue
    return imported


def load_env() -> None:
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.exists():
        return

    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


load_env()
configure_logging()
logger = logging.getLogger("karnex.api")
try:
    IST = ZoneInfo("Asia/Kolkata")
except ZoneInfoNotFoundError:
    # Windows Python may miss tzdata package; fallback keeps IST behavior stable.
    IST = timezone(timedelta(hours=5, minutes=30))


def _interview_mode_from_job(job: dict | None) -> str:
    """Canonical technical | hr from job template or session config."""
    if not job:
        return "technical"
    return normalize_interview_mode(job.get("interviewMode") or job.get("interview_mode"))


def _template_generation_options(
    *,
    edited: str,
    generated: str,
    effective: str,
    form_skills: list[str],
) -> tuple[bool, list[str]]:
    """
    When HR replaces the default prompt, honor only that text for generation.
    Do not force form-field skills into validation unless the prompt lists Skills:.
    """
    from template_prompt import is_custom_edited_prompt

    is_custom = is_custom_edited_prompt(edited, generated)
    if not is_custom:
        return False, list(form_skills or [])
    prompt_skills = _skills_from_template_prompt(effective)
    if prompt_skills:
        return True, prompt_skills
    return True, []


def _template_generation_options_from_job(
    job: dict | None,
    *,
    effective: str,
    form_skills: list[str],
) -> tuple[bool, list[str]]:
    j = job or {}
    ctx = _template_prompt_context_from_job(j)
    generated = sanitize_prompt_input(str(j.get("generatedPrompt") or ""))
    if not generated:
        generated = build_default_template_prompt(ctx)
    edited = sanitize_prompt_input(str(j.get("editedPrompt") or ""))
    if edited == generated:
        edited = ""
    return _template_generation_options(
        edited=edited,
        generated=generated,
        effective=effective,
        form_skills=form_skills,
    )


def _generate_interview_questions(
    *,
    interview_mode: str,
    jd_text: str,
    cv_text: str,
    difficulty: str,
    n: int,
    model: str,
    skills: list,
    coach_hints: str = "",
    experience: str = "",
    domain_categories: list | None = None,
    raw_passthrough: bool = False,
    temperature: float = 0.45,
    variety_seed: str = "",
    role: str = "",
    tech_stack: str = "",
    avoid_history: list | None = None,
    template_prompt: str = "",
    template_custom: bool = False,
    validation_skills: list | None = None,
) -> list[str]:
    """Mode-aware question generation with OpenAI + fallback."""
    merged_hints = sanitize_prompt_input(template_prompt, max_chars=8000).strip()
    if not merged_hints:
        merged_hints = (coach_hints or "").strip()
    is_custom = template_custom
    val_skills = validation_skills
    if val_skills is None and not is_custom:
        val_skills = list(skills or [])
    return generate_mode_aware_questions(
        interview_mode=interview_mode,
        skills=skills,
        experience=experience,
        role=role,
        difficulty=difficulty,
        question_count=n,
        tech_stack=tech_stack,
        resume_summary=cv_text,
        jd_text=jd_text,
        cv_text=cv_text,
        model=model,
        coach_hints=merged_hints,
        avoid_history=avoid_history,
        domain_categories=domain_categories,
        raw_passthrough=raw_passthrough,
        temperature=temperature,
        variety_seed=variety_seed,
        template_custom=is_custom,
        validation_skills=val_skills,
    )


def _build_question_avoid_history(
    job: dict | None,
    weights: dict | None = None,
    *,
    session_asked: list | None = None,
) -> list[str]:
    """
    Anti-repetition bundle for question generation (May 2026).

    Merges template manual/preview lines, prior interviews on the same job_id,
    global recent turns, and the current session asked list.
    """
    w = weights if isinstance(weights, dict) else {}
    if job and not w:
        w = job.get("weights") if isinstance(job.get("weights"), dict) else {}
    job_id = str((job or {}).get("jobId") or (job or {}).get("job_id") or "").strip()
    manual = _normalized_manual_questions_for_job((job or {}).get("manualQuestions"))
    preview: list[str] = []
    raw_prev = w.get("previewQuestions")
    if isinstance(raw_prev, list):
        preview = [str(q).strip() for q in raw_prev if str(q).strip()]
    job_recent: list[str] = []
    if job_id:
        try:
            job_recent = recent_questions_for_job_template(AUTH_DB_TARGET, job_id, limit=80)
        except Exception:
            job_recent = []
    # Reduce same-question collisions across concurrent candidates for the same
    # template by also avoiding questions already queued in active sessions.
    active_reserved: list[str] = []
    if job_id:
        try:
            for sess in (sessions or {}).values():
                if not isinstance(sess, dict):
                    continue
                meta = sess.get("meta") if isinstance(sess.get("meta"), dict) else {}
                if str(meta.get("job_id") or "").strip() != job_id:
                    continue
                if bool(sess.get("completed")) or bool(sess.get("submitted")):
                    continue
                qs = sess.get("questions") if isinstance(sess.get("questions"), list) else []
                active_reserved.extend([str(q).strip() for q in qs if str(q).strip()])
        except Exception:
            active_reserved = []
    return build_question_avoid_history(
        global_recent=recently_asked_questions(120),
        job_recent=job_recent,
        manual_questions=manual,
        template_preview=preview,
        session_asked=(list(session_asked or []) + active_reserved),
    )


def _adaptive_followup_enabled() -> bool:
    """Adaptive follow-up UI/workflow removed; kept for explicit env override only."""
    return str(os.getenv("INTERVIEW_FOLLOWUP_MODE", "false")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _now_ist_parts() -> dict:
    now = datetime.now(IST)
    return {
        "ist_iso": now.isoformat(),
        "ist_date": now.strftime("%Y-%m-%d"),
        "ist_time": now.strftime("%H:%M:%S"),
    }


def _auth_db_target() -> str:
    direct = (os.getenv("AUTH_DB_URL") or "").strip()
    if direct:
        from auth_db import normalize_postgres_dsn

        return normalize_postgres_dsn(direct)
    host = (os.getenv("DB_HOST") or "").strip()
    port = (os.getenv("DB_PORT") or "5432").strip()
    name = (os.getenv("DB_NAME") or "").strip()
    user = (os.getenv("DB_USER") or "").strip()
    password = (os.getenv("DB_PASSWORD") or "").strip()
    if host and name and user:
        return f"postgresql://{user}:{password}@{host}:{port}/{name}"
    return str(KARNEX_DB_FILE)


def _auth_secret() -> str:
    """Delegates to auth_secret.py — no local fallback (see that module).

    A default here meant tokens could be forged by anyone reading the source.
    """
    return _shared_auth_secret()


def _token_ttl_minutes() -> int:
    # Candidate interviews can run for several hours; default tokens must not
    # expire mid-session and block /answer or /submit.
    raw = (os.getenv("AUTH_TOKEN_TTL_MIN") or "480").strip()
    try:
        ttl = int(raw)
    except ValueError:
        ttl = 480
    return max(30, min(ttl, 1440))


def _admin_dashboard_dist_path() -> Path:
    return FRONTEND_DIR / "admin-dashboard" / "dist"


def _admin_dashboard_assets_ok() -> bool:
    """Dist must exist and be built with Vite base /admin/ (see frontend/admin-dashboard/vite.config.ts)."""
    idx = _admin_dashboard_dist_path() / "index.html"
    if not idx.is_file():
        return False
    try:
        text = idx.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return "/admin/assets/" in text


def _try_build_admin_dashboard() -> bool:
    """
    After git clone, admin-dashboard/dist is gitignored, so /admin would 404 unless built.
    If Node/npm is available, build automatically once at startup.
    Set SKIP_ADMIN_DASHBOARD_BUILD=1 to skip (e.g. minimal CI smoke tests).
    """
    if str(os.getenv("SKIP_ADMIN_DASHBOARD_BUILD", "")).lower() in {"1", "true", "yes", "on"}:
        return _admin_dashboard_assets_ok()
    if _admin_dashboard_assets_ok():
        return True
    dashboard = FRONTEND_DIR / "admin-dashboard"
    if not dashboard.is_dir() or not (dashboard / "package.json").is_file():
        logger.warning("Admin dashboard sources missing under %s", dashboard)
        return False
    build_lock = dashboard / ".admin_dashboard_building"
    try:
        stale_age = float((os.getenv("ADMIN_DASHBOARD_BUILD_LOCK_STALE_SEC") or "3600").strip() or "3600")
    except ValueError:
        stale_age = 3600.0
    try:
        if build_lock.is_dir():
            age = time.time() - build_lock.stat().st_mtime
            if age > stale_age:
                try:
                    build_lock.rmdir()
                except OSError:
                    pass
    except OSError:
        pass
    try:
        build_lock.mkdir(exist_ok=False)
    except FileExistsError:
        logger.info("Admin dashboard build already in progress in another process; waiting …")
        for _ in range(450):
            if _admin_dashboard_assets_ok():
                return True
            time.sleep(2.0)
        logger.warning("Timed out waiting for another process to finish the admin dashboard build.")
        return _admin_dashboard_assets_ok()
    try:
        return _try_build_admin_dashboard_nolock(dashboard)
    finally:
        try:
            build_lock.rmdir()
        except OSError:
            pass


def _try_build_admin_dashboard_nolock(dashboard: Path) -> bool:
    npm = shutil.which("npm")
    if not npm:
        logger.warning(
            "Admin dashboard is not built (no dist/) and npm was not found on PATH. "
            "Install Node.js LTS, or from repo root run: start_app.bat — "
            "or: cd frontend\\admin-dashboard && npm install && npm run build"
        )
        return False
    lockfile = dashboard / "package-lock.json"
    # Use the RESOLVED npm path: on Windows npm is npm.cmd, and CreateProcess
    # cannot launch the bare "npm" string without a shell (WinError 2).
    install_cmd = [npm, "ci"] if lockfile.is_file() else [npm, "install"]
    logger.info("Building admin dashboard at %s …", dashboard)
    try:
        r = subprocess.run(
            install_cmd,
            cwd=str(dashboard),
            check=False,
            capture_output=True,
            text=True,
            timeout=900,
        )
        if r.returncode != 0:
            tail = (r.stderr or r.stdout or "")[-8000:]
            logger.error("Admin dashboard npm install failed (exit %s). Output tail:\n%s", r.returncode, tail)
            return _admin_dashboard_assets_ok()
        r = subprocess.run(
            [npm, "run", "build"],
            cwd=str(dashboard),
            check=False,
            capture_output=True,
            text=True,
            timeout=900,
        )
        if r.returncode != 0:
            tail = (r.stderr or r.stdout or "")[-8000:]
            logger.error("Admin dashboard npm run build failed (exit %s). Output tail:\n%s", r.returncode, tail)
            return _admin_dashboard_assets_ok()
    except subprocess.TimeoutExpired:
        logger.error("Admin dashboard build timed out after 900s")
        return _admin_dashboard_assets_ok()
    except OSError as exc:
        logger.error("Admin dashboard build failed: %s", exc)
        return _admin_dashboard_assets_ok()
    if _admin_dashboard_assets_ok():
        logger.info("Admin dashboard build finished successfully.")
        return True
    logger.error("Admin dashboard build finished but dist/index.html still missing /admin/assets/ references.")
    return False


def _issue_access_token(user: dict, extra_claims: dict | None = None) -> tuple[str, str]:
    now_utc = datetime.now(timezone.utc)
    exp_utc = now_utc + timedelta(minutes=_token_ttl_minutes())
    payload = {
        "sub": user.get("username", ""),
        "role": user.get("role", ""),
        "full_name": user.get("full_name", ""),
        "email": user.get("email", ""),
        "iat": int(now_utc.timestamp()),
        "exp": int(exp_utc.timestamp()),
    }
    if extra_claims:
        for key, val in extra_claims.items():
            if val is not None:
                payload[key] = val
    token = jwt.encode(payload, _auth_secret(), algorithm="HS256")
    exp_ist = exp_utc.astimezone(IST)
    return token, exp_ist.isoformat()


def _decode_token_from_header(request: Request) -> dict | None:
    auth = (request.headers.get("Authorization") or "").strip()
    if not auth.startswith("Bearer "):
        return None
    token = auth[len("Bearer "):].strip()
    if not token:
        return None
    try:
        payload = jwt.decode(
            token,
            _auth_secret(),
            algorithms=["HS256"],
            options={"verify_signature": True, "verify_exp": True, "require": ["exp"]},
        )
        return payload if isinstance(payload, dict) else None
    except jwt.PyJWTError:
        return None


def _detect_primary_lan_ip() -> str:
    """Best-effort LAN IPv4 detection without external network dependency."""
    def _is_usable_ipv4(ip: str) -> bool:
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return False
        if addr.version != 4:
            return False
        return not (addr.is_loopback or addr.is_link_local or addr.is_multicast or addr.is_unspecified)

    def _prefer_private(candidates: list[str]) -> str:
        if not candidates:
            return ""
        for ip in candidates:
            try:
                if ipaddress.ip_address(ip).is_private:
                    return ip
            except ValueError:
                continue
        return candidates[0]

    found: list[str] = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            ip = sock.getsockname()[0]
            if _is_usable_ipv4(ip):
                found.append(ip)
    except Exception:
        pass
    # When there is no default route (offline), the UDP trick may yield 169.254.* or nothing.
    # Fall back to hostname enumeration and prefer RFC1918 private ranges.
    try:
        host = socket.gethostname()
        for info in socket.getaddrinfo(host, None):
            ip = info[4][0]
            if _is_usable_ipv4(ip) and ip not in found:
                found.append(ip)
    except Exception:
        pass
    return _prefer_private(found)


def _private_lan_ipv4(host: str) -> str | None:
    try:
        addr = ipaddress.ip_address((host or "").strip())
    except ValueError:
        return None
    if addr.version != 4:
        return None
    if addr.is_loopback or addr.is_link_local or addr.is_unspecified:
        return None
    if not addr.is_private:
        return None
    return str(addr)


def _invite_host_port_from_request(request: Request) -> tuple[str | None, str]:
    """
    When HR schedules from https://192.168.x.x:PORT in the browser, use that IP for invite links
    (fixes stale PUBLIC_BASE_URL or wrong UDP-detected interface).
    Checks X-Forwarded-Host then Host.
    """
    candidates: list[str] = []
    xf = (request.headers.get("x-forwarded-host") or "").split(",")[0].strip()
    if xf:
        candidates.append(xf)
    ho = (request.headers.get("host") or "").strip()
    if ho:
        candidates.append(ho)

    for raw in candidates:
        if "[" in raw:
            continue
        host_part = raw
        port_suffix = ""
        if raw.count(":") == 1:
            host_part, maybe_p = raw.rsplit(":", 1)
            if maybe_p.isdigit():
                port_suffix = f":{maybe_p}"
        ip = _private_lan_ipv4(host_part.strip())
        if ip:
            return ip, port_suffix

    return None, ""


def _public_frontend_base_url() -> str:
    pub = (os.getenv("PUBLIC_BASE_URL") or "").strip().rstrip("/")
    if pub and pub.lower() != "auto":
        return pub
    return ""


def _invite_base_url(request: Request) -> str:
    """
    Invite URL precedence:
    1) PUBLIC_BASE_URL when set (production frontend — Vercel, etc.).
    2) Browser Host / X-Forwarded-Host when it is a private LAN IPv4 (local HR dev).
    3) Auto-detect LAN IPv4 when request URL uses localhost/link-local.
    4) X-Forwarded-Host from reverse proxy when not the API host.
    5) Request base URL.
    """
    pub = _public_frontend_base_url()
    if pub:
        return pub

    req_url = str(request.base_url).rstrip("/")
    parsed = urlparse(req_url)
    scheme = parsed.scheme or "http"
    port_fallback = f":{parsed.port}" if parsed.port else ""

    lan_ip, port_hdr = _invite_host_port_from_request(request)
    if lan_ip:
        return f"{scheme}://{lan_ip}{port_hdr or port_fallback}"

    host = parsed.hostname or ""
    port = port_fallback
    bad_hosts = {"", "localhost", "127.0.0.1", "::1", "0.0.0.0"}
    if host in bad_hosts or host.startswith("169.254."):
        detected = _detect_primary_lan_ip()
        if detected:
            return f"{scheme}://{detected}{port}"

    fwd_host = (request.headers.get("x-forwarded-host") or "").split(",")[0].strip()
    if fwd_host and "onrender.com" not in fwd_host.lower():
        fwd_proto = (request.headers.get("x-forwarded-proto") or scheme or "https").strip()
        return f"{fwd_proto}://{fwd_host}"

    return req_url


def _session_key_from_payload(payload: dict | None) -> str:
    """Invite candidates use per-token keys; HR demo uses per-user keys."""
    if not payload:
        return SESSION_ID
    tok = str(payload.get("invite_token") or "").strip()
    if tok:
        return f"inv:{tok}"
    sub = str(payload.get("sub") or "").strip()
    if sub:
        return f"hr:{sub}"
    return SESSION_ID


def _session_key_from_session(session: dict | None) -> str:
    meta = (session or {}).get("meta", {}) or {}
    tok = str(meta.get("invite_token") or "").strip()
    if tok:
        return f"inv:{tok}"
    hr_user = str(meta.get("hr_username") or meta.get("created_by") or "").strip()
    if hr_user:
        return f"hr:{hr_user}"
    return SESSION_ID


def _is_production_env() -> bool:
    env = str(os.getenv("KARNEX_ENV") or os.getenv("ENV") or os.getenv("NODE_ENV") or "").strip().lower()
    return env in {"production", "prod"}


def _default_auth_secrets() -> set[str]:
    return {
        "change-me-auth-secret",
        "change_this_jwt_secret",
        "apple",
        REPORT_CODE,
    }


def _auth_secret_is_default() -> bool:
    raw = (os.getenv("AUTH_SECRET") or "").strip()
    report = (os.getenv("REPORT_CODE") or "").strip()
    if raw and raw.lower() not in _default_auth_secrets():
        return False
    if report and report.lower() not in _default_auth_secrets():
        return False
    if not raw and not report:
        return True
    return bool(raw in _default_auth_secrets() or report in _default_auth_secrets())


def _allow_public_hr_registration() -> bool:
    """Whether an anonymous visitor may self-register with role=hr.

    This must stay OFF in any deployment reachable from outside: the HR role
    unlocks the candidate database, prompt logs and the question bank, so a
    single POST would hand an attacker the whole system. It exists only for
    first-run local setup, and is deliberately opt-in per environment.
    """
    if _is_production_env():
        return False
    return str(os.getenv("ALLOW_PUBLIC_HR_REGISTRATION", "false")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _persist_hr_record_mirror(record: dict) -> None:
    """Postgres is authoritative; JSON file is a best-effort mirror."""
    upsert_hr_record_async(DATA_FILE, record)
    _crm_sync_interview_async(record)


def _crm_sync_interview_async(record: dict) -> None:
    """Karnex CRM write-back (Phase 5): when this interview belongs to a CRM
    candidate profile (ai_interview_links, joined by invite_token), push the
    score/result into the CRM in a background thread. No-op for standalone
    interviews or when the CRM is unconfigured; never raises."""
    def _run() -> None:
        try:
            from services.ai_interview_bridge import sync_completed_interview
            sync_completed_interview(record)
        except Exception as exc:  # pragma: no cover
            logger.warning("CRM interview sync skipped: %s", exc)

    threading.Thread(target=_run, daemon=True, name="crm-interview-sync").start()


def _enforce_invite_device_binding(request: Request, payload: dict | None) -> JSONResponse | None:
    """Require x-device-id to match schedule.active_device_id for bound invite sessions."""
    invite_token = str((payload or {}).get("invite_token") or "").strip()
    if not invite_token:
        return None
    record = get_schedule_by_token(AUTH_DB_TARGET, invite_token)
    if not record:
        return None
    active_device = str(record.get("active_device_id") or "").strip()
    if not active_device:
        return None
    request_device = str(request.headers.get("x-device-id") or "").strip()
    if not request_device or request_device != active_device:
        return JSONResponse(
            {"error": "Device verification failed. This interview is bound to another device."},
            status_code=403,
        )
    return None



def _build_answer_response(session: dict, *, is_skipped_answer: bool) -> dict:
    answers = session.get("answers") or []
    cur = int(session.get("current", 0) or 0)
    payload_session = session
    if len(answers) > cur:
        payload_session = {**session, "current": len(answers)}
    next_payload = None
    if not payload_session.get("finalizing"):
        next_payload = next_question_payload(payload_session)
    return {
        "status": "ok",
        "answered": len(answers),
        "skipped": bool(is_skipped_answer),
        "next": next_payload,
        "idempotent": True,
    }


def _pack_invite_config_into_notes(notes: str, cfg: dict) -> str:
    clean_notes = (notes or "").strip()
    payload = {
        "final_skills": [s.strip().lower() for s in (cfg.get("final_skills") or []) if str(s).strip()],
        "num_q": clamp_count_mode_questions(cfg.get("num_q") or 5),
        "difficulty": str(cfg.get("difficulty") or "medium").strip().lower() or "medium",
        "followup_mode": bool(cfg.get("followup_mode", False)),
        "timing_mode": str(cfg.get("timing_mode") or "count").strip().lower() or "count",
        "time_limit_sec": max(0, int(cfg.get("time_limit_sec") or 0)),
        "mic_always_on": bool(cfg.get("mic_always_on", False)),
        "show_spoken_text": bool(cfg.get("enable_transcript_input", cfg.get("show_spoken_text", False))),
        "enable_transcript_input": bool(cfg.get("enable_transcript_input", cfg.get("show_spoken_text", False))),
        "model": str(cfg.get("model") or "gpt-4o-mini").strip() or "gpt-4o-mini",
        "job_id": str(cfg.get("job_id") or cfg.get("jobId") or "").strip(),
    }
    marker = "__KARNEX_CFG__:"
    base = clean_notes.split(marker, 1)[0].strip()
    return f"{base}\n{marker}{json.dumps(payload, ensure_ascii=True)}".strip()


def _extract_invite_config_from_notes(notes: str) -> dict:
    marker = "__KARNEX_CFG__:"
    raw = str(notes or "")
    if marker not in raw:
        return {}
    cfg_raw = raw.split(marker, 1)[1].strip()
    try:
        cfg = json.loads(cfg_raw)
        if not isinstance(cfg, dict):
            return {}
        return cfg
    except Exception:
        return {}


INTELLIGENCE_SUITE_CATEGORY_GUIDE: dict[str, str] = {
    "fundamental": "Fundamental / Core Concept",
    "scenario": "Scenario-Based",
    "debugging": "Debugging & Troubleshooting",
    "hands-on": "Hands-On Implementation",
    "deep-dive": "Project Deep-Dive",
    "adaptive": "Adaptive Follow-Up",
    "oem": "OEM / Production-Level",
    "architecture": "Architecture & Design",
    "logic": "Coding / Logic",
    "communication": "Communication & Explanation",
    "behavioral": "Behavioral",
    "leadership": "Leadership / Managerial",
}


def _domain_title_from_category_id(cid: str) -> str:
    key = str(cid or "").strip()
    if not key:
        return ""
    return INTELLIGENCE_SUITE_CATEGORY_GUIDE.get(key) or key


def _resolve_domain_titles(category_ids: list[str]) -> list[str]:
    out: list[str] = []
    for cid in category_ids:
        title = _domain_title_from_category_id(cid)
        if title and title not in out:
            out.append(title)
    return out


def _intelligence_suite_prefix_from_weights(weights: dict | None, *, base_role_fallback: str = "") -> str:
    """Build INTELLIGENCE SUITE JD prefix from template weights (empty if no suite config)."""
    w = weights if isinstance(weights, dict) else {}
    raw = w.get("questionCategories")
    cat_ids: list[str] = []
    if isinstance(raw, list):
        cat_ids = [str(x).strip() for x in raw if str(x).strip()]
    role = str(w.get("intelligenceTargetRole") or "").strip() or str(base_role_fallback or "").strip()
    seniority = str(w.get("intelligenceSeniority") or "").strip()
    stack = str(w.get("intelligenceTechStack") or "").strip()
    suite_lines: list[str] = []
    if role:
        suite_lines.append(f"Target role: {role}")
    if seniority:
        suite_lines.append(f"Seniority level: {seniority}")
    if stack:
        suite_lines.append(f"Technical stack emphasis: {stack}")
    if cat_ids:
        suite_lines.append("Questionnaire must emphasize these assessment domains (spread across all questions):")
        for cid in cat_ids:
            title = _domain_title_from_category_id(cid)
            if title:
                suite_lines.append(f"- {title}")
    if not suite_lines:
        return ""
    return "INTERVIEW INTELLIGENCE SUITE CONTEXT\n" + "\n".join(suite_lines) + "\n\n---\n\n"


def _jd_with_intelligence_suite(jd_base: str, weights: dict | None, *, role_fallback: str = "") -> str:
    prefix = _intelligence_suite_prefix_from_weights(weights, base_role_fallback=role_fallback)
    if not prefix:
        return (jd_base or "").strip()
    return prefix + (jd_base or "").strip()


def _template_instructions_from_job(job: dict | None) -> str:
    """AI prompt template instructions (separate from JD text)."""
    j = job or {}
    ti = str(j.get("templateInstructions") or j.get("template_instructions") or "").strip()
    if ti:
        return ti
    return str(j.get("jdText") or "").strip()


def _resolve_template_instructions_param(template_instructions: str = "", jd_text: str = "") -> str:
    ti = str(template_instructions or "").strip()
    if ti:
        return ti
    return str(jd_text or "").strip()


def _template_prompt_context_from_job(job: dict | None) -> dict[str, str]:
    j = job or {}
    weights = j.get("weights") if isinstance(j.get("weights"), dict) else {}
    exp_min = int(j.get("expMin") or 0)
    exp_max = int(j.get("expMax") or 0)
    exp_label = f"{exp_min}-{exp_max} years" if exp_max > 0 else (f"{exp_min}+ years" if exp_min > 0 else "Not specified")
    return build_template_prompt_context(
        role=str(j.get("jobTitle") or "").strip(),
        experience=exp_label,
        required_skills=j.get("requiredSkills") or [],
        optional_skills=j.get("optionalSkills") or [],
        difficulty=str(j.get("difficulty") or "medium"),
        interview_type=str(j.get("interviewMode") or "technical"),
        customer_name=str(j.get("customerName") or ""),
        opportunity_id=str(j.get("opportunityId") or ""),
        template_instructions=_template_instructions_from_job(j),
        technology_stack=str((weights or {}).get("intelligenceTechStack") or ""),
        interview_mode=str(j.get("interviewMode") or "technical"),
    )


def _effective_template_prompt(job: dict | None) -> str:
    j = job or {}
    ctx = _template_prompt_context_from_job(j)
    generated = sanitize_prompt_input(str(j.get("generatedPrompt") or ""))
    if not generated:
        generated = build_default_template_prompt(ctx)
    edited = sanitize_prompt_input(str(j.get("editedPrompt") or ""))
    if edited == generated:
        edited = ""
    effective = edited or generated
    return render_prompt_preview(effective, ctx)


def _prompt_line_value(prompt: str, key: str) -> str:
    """Extract 'Key: value' from a multi-line prompt (case-insensitive)."""
    text = str(prompt or "")
    k = str(key or "").strip()
    if not text or not k:
        return ""
    m = re.search(rf"(?im)^[ \t]*{re.escape(k)}[ \t]*:[ \t]*(.+?)\s*$", text)
    return (m.group(1).strip() if m else "")


def _skills_from_template_prompt(prompt: str) -> list[str]:
    raw = _prompt_line_value(prompt, "Skills")
    if not raw:
        return []
    if raw.strip().lower() in {"not specified", "none", "-"}:
        return []
    items = [s.strip() for s in raw.split(",")]
    out: list[str] = []
    seen: set[str] = set()
    for it in items:
        s = " ".join(str(it or "").split()).strip()
        if not s:
            continue
        low = s.lower()
        if low in seen:
            continue
        seen.add(low)
        out.append(s)
    return out[:15]


def _shuffle_manual_questions_for_session(questions: list[str], *seed_parts: str) -> list[str]:
    """
    Same template manual list, different order per interview session.

    Uses a deterministic seed from session-specific material so concurrent
    candidates get uncorrelated sequences without storing a separate permutation table.
    """
    if len(questions) <= 1:
        return list(questions)
    raw = "\x1e".join(str(p or "") for p in seed_parts)
    seed_int = int.from_bytes(hashlib.sha256(raw.encode("utf-8")).digest()[:8], "big")
    rng = random.Random(seed_int)
    out = list(questions)
    rng.shuffle(out)
    return out


_INVITE_BOOTSTRAP_LOCKS: dict[str, threading.Lock] = {}
_INVITE_BOOTSTRAP_LOCKS_GUARD = threading.Lock()
_INVITE_PREWARM_STATE: dict[str, dict] = {}
_INVITE_PREWARM_STATE_GUARD = threading.Lock()
_INVITE_PREWARM_AHEAD_SEC = 24 * 60 * 60


def _invite_token_tag(invite_token: str) -> str:
    tok = str(invite_token or "").strip()
    if not tok:
        return ""
    return f"{tok[:8]}...{tok[-4:]}" if len(tok) > 12 else tok


def _invite_bootstrap_lock(invite_token: str) -> threading.Lock:
    with _INVITE_BOOTSTRAP_LOCKS_GUARD:
        lock = _INVITE_BOOTSTRAP_LOCKS.get(invite_token)
        if lock is None:
            lock = threading.Lock()
            _INVITE_BOOTSTRAP_LOCKS[invite_token] = lock
        return lock


def _set_invite_prewarm_state(invite_token: str, **fields) -> None:
    with _INVITE_PREWARM_STATE_GUARD:
        base = dict(_INVITE_PREWARM_STATE.get(invite_token) or {})
        base.update(fields)
        _INVITE_PREWARM_STATE[invite_token] = base


def _invite_prewarm_snapshot(invite_token: str) -> dict:
    with _INVITE_PREWARM_STATE_GUARD:
        return dict(_INVITE_PREWARM_STATE.get(invite_token) or {})


def _run_invite_prewarm(invite_token: str, schedule: dict, reason: str) -> None:
    started = time.time()
    _set_invite_prewarm_state(
        invite_token,
        status="running",
        reason=reason,
        started_at=datetime.now(timezone.utc).isoformat(),
        latency_ms=0,
        error="",
    )
    try:
        result = _bootstrap_invite_interview_session(invite_token, schedule)
        if result.get("error"):
            _set_invite_prewarm_state(
                invite_token,
                status="error",
                error=str(result.get("error") or "prewarm failed"),
                completed_at=datetime.now(timezone.utc).isoformat(),
                latency_ms=int((time.time() - started) * 1000),
            )
            return
        _set_invite_prewarm_state(
            invite_token,
            status="ready",
            error="",
            completed_at=datetime.now(timezone.utc).isoformat(),
            latency_ms=int((time.time() - started) * 1000),
            question_count=int(result.get("question_count") or 0),
            session_key=str(result.get("session_key") or ""),
        )
        # Questions exist now, and the candidate is still on the welcome/device
        # -test screens. Synthesise the opening question in that dead time so it
        # plays instantly when they reach the interview.
        _prewarm_opening_audio(str(result.get("session_key") or "") or f"inv:{invite_token}")
        logger.info(
            "interview.invite.prewarm.ready",
            extra={
                "event": "interview.invite.prewarm.ready",
                "invite_token": _invite_token_tag(invite_token),
                "reason": reason,
                "latency_ms": int((time.time() - started) * 1000),
                "question_count": int(result.get("question_count") or 0),
                "reused": bool(result.get("reused")),
            },
        )
    except Exception as exc:
        _set_invite_prewarm_state(
            invite_token,
            status="error",
            error=str(exc),
            completed_at=datetime.now(timezone.utc).isoformat(),
            latency_ms=int((time.time() - started) * 1000),
        )
        logger.warning(
            "interview.invite.prewarm.error",
            extra={
                "event": "interview.invite.prewarm.error",
                "invite_token": _invite_token_tag(invite_token),
                "reason": reason,
                "error": str(exc),
            },
        )


def _try_restore_invite_session(invite_token: str) -> dict | None:
    """Restore in-memory session from interview_progress (idempotent invite bootstrap)."""
    progress = get_interview_progress_by_invite(AUTH_DB_TARGET, invite_token)
    if not progress:
        return None
    status = str(progress.get("status") or "").strip().lower()
    if status in {"completed", "terminated", "recovered", "abandoned", "partially_completed"}:
        return None
    sess = _session_from_progress(progress)
    if not isinstance(sess, dict) or not sess.get("questions"):
        return None
    return sess


def _dedupe_integrity_schedule_rows(rows: list[dict]) -> list[dict]:
    """One integrity row per invite_token; collapse duplicate in-flight rows per candidate email."""
    if not rows:
        return []
    by_token: dict[str, dict] = {}
    for row in rows:
        token = str(row.get("invite_token") or "").strip()
        if token:
            by_token[token] = row
    deduped = list(by_token.values()) if by_token else list(rows)
    in_flight = {"pending", "verified", "active"}
    best_active: dict[str, dict] = {}
    rest: list[dict] = []
    for row in deduped:
        status = str(row.get("session_status") or "pending").strip().lower()
        email = str(row.get("candidate_email") or "").strip().lower()
        if status in in_flight and email:
            prev = best_active.get(email)
            if not prev:
                best_active[email] = row
                continue
            def _row_ts(r: dict) -> str:
                return str(
                    r.get("interview_started_at")
                    or r.get("verified_at")
                    or r.get("scheduled_at_local")
                    or r.get("created_at_ist")
                    or ""
                )
            if _row_ts(row) >= _row_ts(prev):
                best_active[email] = row
        else:
            rest.append(row)
    merged = rest + list(best_active.values())
    merged.sort(
        key=lambda r: str(r.get("scheduled_at_local") or r.get("created_at_ist") or ""),
        reverse=True,
    )
    if len(merged) < len(rows):
        logger.info(
            "[INTEGRITY] Deduped schedule rows",
            extra={"event": "integrity.dedupe", "before": len(rows), "after": len(merged)},
        )
    return merged


def _merge_finalize_violations(current_violations: list, prior_events: list) -> list:
    """Merge session + schedule violation events without dropping prior tab switches."""
    merged: list[dict] = []
    seen: set[str] = set()

    def _key(evt: dict) -> str:
        return "|".join(
            [
                str(evt.get("type") or ""),
                str(evt.get("timestamp") or evt.get("at_ist") or ""),
                str(evt.get("details") or "")[:120],
            ]
        )

    for source in (prior_events or [], current_violations or []):
        for raw in source:
            if not isinstance(raw, dict):
                continue
            evt = dict(raw)
            k = _key(evt)
            if k in seen:
                continue
            seen.add(k)
            merged.append(evt)
    return merged


def _wait_for_invite_prewarm(invite_token: str, timeout_sec: float = 12.0) -> dict:
    """Block only while a prewarm thread is running; otherwise return immediately."""
    skey = f"inv:{invite_token}"
    if sessions.get(skey):
        return {"status": "ready", "session_key": skey, **_invite_prewarm_snapshot(invite_token)}
    snap = _invite_prewarm_snapshot(invite_token)
    st = str(snap.get("status") or "").strip().lower()
    if st != "running":
        return snap
    deadline = time.time() + max(0.5, float(timeout_sec or 0))
    # Poll tightly. At the old 200 ms interval a session that became ready right
    # after a tick still cost the candidate up to 200 ms of pure sleep on the
    # login path — small, but it is the very first thing they wait for.
    poll = 0.02
    while time.time() < deadline:
        if sessions.get(skey):
            return {"status": "ready", "session_key": skey, **_invite_prewarm_snapshot(invite_token)}
        snap = _invite_prewarm_snapshot(invite_token)
        st = str(snap.get("status") or "").strip().lower()
        if st in {"ready", "error"}:
            if sessions.get(skey):
                return {"status": "ready", "session_key": skey, **snap}
            return snap
        time.sleep(poll)
        # Back off gradually: responsive when the answer is imminent, cheap when
        # generation is genuinely going to take seconds.
        poll = min(poll * 1.5, 0.25)
    return _invite_prewarm_snapshot(invite_token)


def _prewarm_opening_audio(session_key: str | None = None) -> None:
    """Synthesise the first thing the candidate will hear, before they need it.

    The warmup line is a fixed string, so it can be made the moment the invite
    is opened — long before the candidate clears the device test. When a session
    already exists we warm its real first question too. Both go into the same
    cache `/candidate/tts` reads, so the opening question plays with no OpenAI
    call on the critical path.

    Best-effort throughout: a miss just means the normal path runs.
    """
    try:
        from services.tts_prewarm import prewarm_tts
        from utils.warmup import WARMUP_QUESTION_TEXT, warmup_enabled

        if warmup_enabled():
            prewarm_tts(WARMUP_QUESTION_TEXT)

        if session_key:
            session = sessions.get(session_key) or {}
            questions = session.get("questions") or []
            if questions:
                prewarm_tts(str(questions[0] or ""))
    except Exception:
        pass


def _maybe_prewarm_invite_session(invite_token: str, record: dict, reason: str = "lookup") -> None:
    if not invite_token or not record:
        return
    skey = f"inv:{invite_token}"
    # The opening line is known regardless of question generation — start it now.
    _prewarm_opening_audio(skey if sessions.get(skey) else None)
    if sessions.get(skey):
        _set_invite_prewarm_state(invite_token, status="ready", reason=reason, reused=True, latency_ms=0)
        return
    access = _invite_access_state(record)
    if access.get("reason") == "expired":
        return
    seconds_until = int(access.get("seconds_until_start", 0) or 0)
    if seconds_until > _INVITE_PREWARM_AHEAD_SEC:
        return
    snap = _invite_prewarm_snapshot(invite_token)
    if snap.get("status") in {"running", "ready"}:
        return
    t = threading.Thread(
        target=_run_invite_prewarm,
        args=(invite_token, dict(record), reason),
        daemon=True,
        name=f"invite-prewarm-{invite_token[:8]}",
    )
    t.start()


def _bootstrap_invite_interview_session(invite_token: str, schedule: dict, *, fast_only: bool = False) -> dict:
    """Build questions from saved job config + OpenAI when a candidate opens an invite link.

    When fast_only=True, skip OpenAI and use preview/manual/fallback questions so login
    returns in under a few seconds (used on candidate login; prewarm may still run full AI).
    """
    bootstrap_started = time.time()
    skey = f"inv:{invite_token}"
    lock = _invite_bootstrap_lock(invite_token)
    with lock:
        if sessions.get(skey):
            logger.info(
                "[SESSION] Existing Session Reused",
                extra={"event": "session.reused.memory", "invite_token": _invite_token_tag(invite_token)},
            )
            return {"status": "ok", "session_key": skey, "reused": True}

        restored = _try_restore_invite_session(invite_token)
        if restored:
            sessions[skey] = restored
            logger.info(
                "[SESSION] Existing Session Reused",
                extra={"event": "session.reused.progress", "invite_token": _invite_token_tag(invite_token)},
            )
            return {"status": "ok", "session_key": skey, "reused": True, "restored": True}

        invite_cfg = _extract_invite_config_from_notes(str(schedule.get("notes", "")))
        jid_from_invite = str(invite_cfg.get("job_id") or invite_cfg.get("jobId") or "").strip()
        env_job_id = (os.getenv("INTERVIEW_JOB_ID") or "").strip()
        job = None
        if jid_from_invite:
            job = get_job_template(AUTH_DB_TARGET, jid_from_invite)
        if not job and env_job_id:
            job = get_job_template(AUTH_DB_TARGET, env_job_id)
        if not job:
            jobs = list_job_templates(AUTH_DB_TARGET)
            job = jobs[0] if jobs else None

        jd_text = (job or {}).get("jdText") or (
            "Technical interview for the open role. Assess practical depth, trade-offs, and communication."
        )
        job_title = str((job or {}).get("jobTitle") or "Open position").strip()
        weights = (job or {}).get("weights") if isinstance((job or {}).get("weights"), dict) else {}
        effective_prompt = _effective_template_prompt(job)
        jd_augmented = _jd_with_intelligence_suite(jd_text, weights, role_fallback=job_title)
        req = list((job or {}).get("requiredSkills") or [])
        opt = list((job or {}).get("optionalSkills") or [])
        cfg_skills = [s.strip().lower() for s in (invite_cfg.get("final_skills") or []) if str(s).strip()]
        selected_skills = cfg_skills[:15] if cfg_skills else list(dict.fromkeys([*req, *opt]))[:15]
        # If HR edited the prompt and changed the Skills: line, treat that as
        # authoritative for live question generation so the prompt preview matches
        # runtime behavior.
        prompt_skills = _skills_from_template_prompt(effective_prompt)
        if prompt_skills:
            selected_skills = prompt_skills[:15]
        if not selected_skills:
            selected_skills = infer_interview_skills(jd_text, "")[:12]
        if not selected_skills:
            selected_skills = [
                "communication",
                "problem solving",
                "technical depth",
                "collaboration",
                "ownership",
            ]
        selected_skills = [s for s in selected_skills if s][:15]
        template_custom, validation_skills = _template_generation_options_from_job(
            job,
            effective=effective_prompt,
            form_skills=selected_skills,
        )

    has_ai = openai_key_configured("question")
    selected_model = str(invite_cfg.get("model") or (os.getenv("INTERVIEW_OPENAI_MODEL") or "gpt-4o-mini")).strip()
    safe_mode_on = str(os.getenv("INTERVIEW_SAFE_MODE", "false")).lower() in {"1", "true", "yes", "on"}
    followup_mode_on = _adaptive_followup_enabled() and bool(invite_cfg.get("followup_mode", False))
    difficulty = str(invite_cfg.get("difficulty") or (os.getenv("INTERVIEW_DIFFICULTY") or "medium")).strip().lower() or "medium"
    num_q = resolve_template_num_q(
        invite_cfg,
        job,
        env_num=os.getenv("INTERVIEW_NUM_QUESTIONS"),
        skills_fallback=len(selected_skills) or 5,
    )
    timing_mode = str(invite_cfg.get("timing_mode") or "count").strip().lower() or "count"
    if timing_mode not in {"count", "time"}:
        timing_mode = "count"
    time_limit_sec = max(0, min(int(invite_cfg.get("time_limit_sec") or 0), 6 * 60 * 60))
    pool_q = pool_questions_for_timing(num_q, timing_mode, time_limit_sec=time_limit_sec)
    mic_always_on = bool(invite_cfg.get("mic_always_on", False))
    if "enable_transcript_input" in invite_cfg:
        show_spoken_text = bool(invite_cfg.get("enable_transcript_input"))
    elif "show_spoken_text" in invite_cfg:
        show_spoken_text = bool(invite_cfg.get("show_spoken_text"))
    else:
        show_spoken_text = bool((job or {}).get("enableTranscriptInput", (job or {}).get("showSpokenText", False)))
    coach_hints_text()

    exp_min = int((job or {}).get("expMin") or 0)
    exp_max = int((job or {}).get("expMax") or 0)
    invite_experience = f"{exp_min}-{exp_max} years" if exp_max > 0 else ""
    seniority_w = str(weights.get("intelligenceSeniority") or "").strip()
    if seniority_w:
        invite_experience = f"{seniority_w}; {invite_experience}".strip("; ")

    jd_skills = extract_jd_skills(jd_text)
    cv_skills: list = []

    invite_domains: list[str] = []
    raw_cat_ids = weights.get("questionCategories")
    if isinstance(raw_cat_ids, list):
        invite_domains = _resolve_domain_titles([str(cid).strip() for cid in raw_cat_ids if str(cid).strip()])

    cname = str(schedule.get("candidate_name", "Candidate")).strip() or "Candidate"
    cemail = str(schedule.get("candidate_email", "")).strip() or "candidate@local"
    existing_progress = get_interview_progress_by_invite(AUTH_DB_TARGET, invite_token)
    if existing_progress and str(existing_progress.get("interview_id") or "").strip():
        interview_id = str(existing_progress.get("interview_id") or "").strip()
        logger.info(
            "[SESSION] Existing Session Reused",
            extra={"event": "session.reused.interview_id", "interview_id": interview_id},
        )
    else:
        interview_id = str(uuid4())
        logger.info(
            "[SESSION] Session Created",
            extra={"event": "session.created", "interview_id": interview_id, "invite_token": _invite_token_tag(invite_token)},
        )
    created_ist = _now_ist_parts()
    question_seed = make_question_session_seed(
        cemail.lower(),
        interview_id,
        invite_token,
        str(schedule.get("scheduled_at_local") or ""),
    )

    qt_inv = _coerce_question_type((job or {}).get("questionType"))
    manual_list_inv = _normalized_manual_questions_for_job((job or {}).get("manualQuestions"))
    is_manual_invite = bool(job) and qt_inv == "manual"

    used_saved_preview = False
    if is_manual_invite:
        if not manual_list_inv:
            return {
                "error": (
                    "This interview template uses Manual Questions, but none are saved. "
                    "HR must edit the job template and add at least one interview question line."
                )
            }
        questions = list(manual_list_inv)
        # Shuffling used to be unconditional. RMG can now ask for the saved
        # order instead, which matters when the questions build on each other —
        # asking question 4 before question 1 makes the flow nonsense.
        if should_shuffle_manual_questions(weights):
            questions = _shuffle_manual_questions_for_session(
                questions,
                interview_id,
                str(invite_token or ""),
                cemail.lower(),
                str((job or {}).get("jobId") or ""),
                str(schedule.get("scheduled_at_local") or ""),
            )
        job_timing = str((job or {}).get("timingMode") or (job or {}).get("timing_mode") or timing_mode or "count").strip().lower()
        if job_timing == "count":
            ask_n = clamp_count_mode_questions(
                invite_cfg.get("num_q") or (job or {}).get("numQ") or (job or {}).get("num_q") or num_q or 5
            )
            if len(questions) > ask_n:
                questions = questions[:ask_n]
        pool_q = len(questions)
        num_q = len(questions)
        used_saved_preview = True
    else:
        saved_preview = weights.get("previewQuestions")
        preview_list: list[str] = []
        if isinstance(saved_preview, list):
            preview_list = [str(q).strip() for q in saved_preview if str(q).strip()]

        used_saved_preview = bool(preview_list)
        avoid_hist = _build_question_avoid_history(job, weights)
        if preview_list and has_ai and not safe_mode_on and not fast_only:
            # Fresh AI batch guided by HR preview lines but not a verbatim copy.
            inv_mode = _interview_mode_from_job(job)
            inv_role = str((job or {}).get("jobTitle") or weights.get("intelligenceTargetRole") or "").strip()
            inv_stack = str(weights.get("intelligenceTechStack") or "").strip()
            questions = _generate_interview_questions(
                interview_mode=inv_mode,
                jd_text=jd_augmented,
                cv_text="",
                difficulty=difficulty,
                n=pool_q,
                model=selected_model,
                skills=selected_skills,
                coach_hints="",
                experience=invite_experience,
                domain_categories=invite_domains or None,
                role=inv_role,
                tech_stack=inv_stack,
                template_prompt=effective_prompt,
                variety_seed=question_seed,
                avoid_history=avoid_hist + preview_list,
                template_custom=template_custom,
                validation_skills=validation_skills,
            )
        elif preview_list:
            questions = prepare_unique_question_sequence(
                preview_list,
                seed=question_seed,
                asked_questions=avoid_hist,
                limit=pool_q,
            )
        elif has_ai and not safe_mode_on and not fast_only:
            inv_mode = _interview_mode_from_job(job)
            inv_role = str((job or {}).get("jobTitle") or weights.get("intelligenceTargetRole") or "").strip()
            inv_stack = str(weights.get("intelligenceTechStack") or "").strip()
            questions = _generate_interview_questions(
                interview_mode=inv_mode,
                jd_text=jd_augmented,
                cv_text="",
                difficulty=difficulty,
                n=pool_q,
                model=selected_model,
                skills=selected_skills,
                coach_hints="",
                experience=invite_experience,
                domain_categories=invite_domains or None,
                role=inv_role,
                tech_stack=inv_stack,
                template_prompt=effective_prompt,
                variety_seed=question_seed,
                avoid_history=avoid_hist,
                template_custom=template_custom,
                validation_skills=validation_skills,
            )
        elif template_custom and not fast_only:
            questions = []
        else:
            questions = generate_questions_fallback(jd_augmented, "", difficulty, pool_q, required_skills=selected_skills)

        if used_saved_preview:
            # Keep template preview exactly as HR saved it (no length cap, no skill-based replacement).
            questions = [q for q in questions if q][:pool_q]
        else:
            questions = [q for q in questions if q and len(q) <= 220][:pool_q]
            if selected_skills and questions and not template_custom:
                missing = [sk for sk in selected_skills if not any(question_matches_skill(q, sk) for q in questions)]
                for idx, sk in enumerate(missing):
                    replacement = generate_questions_fallback(jd_augmented, "", difficulty, 1, required_skills=[sk])[0]
                    rep_idx = (len(questions) - 1 - idx) % max(len(questions), 1)
                    questions[rep_idx] = replacement
    if not questions and fast_only:
        questions = generate_questions_fallback(jd_augmented, "", difficulty, pool_q, required_skills=selected_skills)
    if not questions:
        return {"error": "Could not generate interview questions. Set OPENAI_API_KEY and configure a job (HR → ATS / job config), or set INTERVIEW_SAFE_MODE=false with a valid key."}

    questions = prepare_unique_question_sequence(questions, seed=question_seed, limit=pool_q)
    from utils.question_uniqueness import dedupe_question_list_semantic, record_generated_questions_batch

    questions = dedupe_question_list_semantic(questions)
    if not questions:
        return {"error": "Could not generate unique interview questions for this session."}

    # Issue 2 (May 2026): prepend a non-evaluated "introduce yourself" warmup
    # so the candidate can settle in / verify audio before scored questions
    # begin. The injected indices live in meta["warmup_indices"] and are
    # filtered out of evaluation + analytics later in this module.
    questions, warmup_indices = inject_warmup(questions)
    questions = trim_questions_for_count_mode(
        questions,
        num_q,
        timing_mode,
        warmup_count=len(warmup_indices or []),
    )

    candidate_profile = {"name": cname, "email": cemail, "invite": True, "role_hint": job_title}
    raw_qc = weights.get("questionCategories")
    question_categories = [str(x).strip() for x in raw_qc] if isinstance(raw_qc, list) else []

    with lock:
        if sessions.get(skey):
            return {"status": "ok", "session_key": skey, "reused": True}

        sessions[skey] = {
        "meta": {
            "interview_id": interview_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "created_at_ist": created_ist["ist_iso"],
            "created_date_ist": created_ist["ist_date"],
            "created_time_ist": created_ist["ist_time"],
            "difficulty": difficulty,
            "num_q": num_q,
            "model": selected_model,
            "generation_mode": (
                "manual" if is_manual_invite else ("ai" if has_ai and not safe_mode_on else "fallback")
            ),
            "question_source": "manual" if is_manual_invite else "dynamic",
            "safe_mode": safe_mode_on,
            "candidate_profile": candidate_profile,
            "candidate_experience": invite_experience,
            "jd_skills": selected_skills,
            "jd_skills_detected": jd_skills,
            "cv_skills_detected": cv_skills,
            "jd_text": jd_augmented,
            "jd_text_plain": jd_text,
            "question_categories": question_categories,
            "intelligence_target_role": str(weights.get("intelligenceTargetRole") or "").strip(),
            "intelligence_seniority": seniority_w,
            "intelligence_tech_stack": str(weights.get("intelligenceTechStack") or "").strip(),
            "followup_mode": followup_mode_on,
            "interview_mode": _interview_mode_from_job(job),
            "interview_mode_label": to_display_label(_interview_mode_from_job(job)),
            "timing_mode": timing_mode,
            "time_limit_sec": time_limit_sec,
            "mic_always_on": mic_always_on,
            "show_spoken_text": show_spoken_text,
            "enable_transcript_input": show_spoken_text,
            "session_difficulty": str(difficulty).strip().lower()
            if str(difficulty).strip().lower() in ("easy", "medium", "hard")
            else "medium",
            "followups_added": 0,
            "max_followups": 0 if (is_manual_invite or not followup_mode_on) else max(0, pool_q - len(selected_skills)),
            "job_id": str((job or {}).get("jobId") or ""),
            "job_title": job_title,
            "invite_token": invite_token,
            "scheduled_at_local": str(schedule.get("scheduled_at_local") or "").strip(),
            "question_seed": question_seed,
            "asked_questions": [],
            "pool_generated": len(questions),
            "warmup_indices": warmup_indices,
            "template_prompt": effective_prompt,
            "adaptive_next_question": bool((weights or {}).get("adaptiveNextQuestion", False)),
        },
        "questions": questions,
        "answers": [],
        "current": 0,
        "completed": False,
        "submitted": False,
        }
        stamp_time_warning_settings(sessions[skey]["meta"], weights)
        stamp_auto_advance_settings(sessions[skey]["meta"], weights)
        stamp_template_settings(sessions[skey]["meta"], weights)
        stamp_introduction_question_types(sessions[skey]["meta"], warmup_indices)
        record_generated_questions_batch(
            sessions[skey],
            questions,
            source="manual" if is_manual_invite else "dynamic",
        )
        _persist_interview_progress(sessions[skey], status="started")
        latency_ms = int((time.time() - bootstrap_started) * 1000)
        logger.info(
            "[SESSION] Bootstrap complete",
            extra={
                "event": "interview.invite.bootstrap",
                "interview_id": interview_id,
                "candidate_name": cname,
                "questions": len(questions),
                "fast_only": fast_only,
                "latency_ms": latency_ms,
            },
        )
        return {"status": "ok", "session_key": skey, "question_count": len(questions), "fast_only": fast_only, "latency_ms": latency_ms}


def _require_user(request: Request, allowed_roles: set[str] | None = None):
    payload = _decode_token_from_header(request)
    if not payload:
        return None, JSONResponse({"error": "Unauthorized. Please login again."}, status_code=401)
    role = str(payload.get("role", "")).lower()
    if allowed_roles and role not in allowed_roles:
        return None, JSONResponse({"error": "Forbidden for this role."}, status_code=403)
    return payload, None


#: CRM roles allowed to read interview reports / candidate interview data.
_REPORT_READER_CRM_ROLES = {"TA", "HR", "RMG", "Admin", "CEO"}


def _require_interview_report_reader(request: Request):
    """Auth for CRM-facing interview report / candidate-interview reads.

    The report pages live in the CRM, but the endpoints behind them were written
    for the legacy Interview Platform and gated on the legacy ``role == 'hr'``.
    CRM staff are NOT guaranteed a legacy 'hr' role (a TA/RMG can have legacy
    'candidate'), which locked them out of a feature they are meant to use while
    Admin/CEO — who default to legacy 'hr' — worked fine.

    This accepts:
      * any user whose CRM roles include TA/HR/RMG/Admin/CEO (regardless of legacy role), and
      * legacy 'hr' logins that have no CRM role at all (classic HR platform users).
    It refuses a plain interview-taker token (legacy 'candidate', no CRM staff role).
    Returns ``(payload, error_response)`` like ``_require_user``.
    """
    payload, err = _require_user(request, {"hr", "candidate"})
    if err:
        return None, err
    try:
        from crm_deps import _roles_for_username
        crm_roles = _roles_for_username(str(payload.get("sub") or ""))
    except Exception:
        crm_roles = None
    if crm_roles:  # user has CRM roles → must include a report-reader role
        if crm_roles & _REPORT_READER_CRM_ROLES:
            return payload, None
        return None, JSONResponse({"error": "Forbidden for this role."}, status_code=403)
    # No CRM role (or CRM unavailable) → fall back to legacy: allow only 'hr'.
    if str(payload.get("role", "")).lower() == "hr":
        return payload, None
    return None, JSONResponse({"error": "Forbidden for this role."}, status_code=403)


def _actor_label(request: Request) -> str | None:
    """Best-effort "who did this" for audit trails, from the bearer token."""
    try:
        payload = _decode_token_from_header(request) or {}
    except Exception:
        return None
    for key in ("email", "username", "name", "sub"):
        value = str(payload.get(key) or "").strip()
        if value:
            return value[:255]
    return None


def _push_decision_to_crm(*, decision, actor, interview_record_id=None,
                          invite_token=None, candidate_email=None) -> None:
    """Mirror a recruiter's interview decision into the CRM. Never raises.

    The report page and the CRM sit in two different databases. Without this the
    CRM only ever knows the AI's score-threshold verdict, which is why an
    interview marked Selected could still show "Failed · 57.2%" on the profile.
    Best-effort by design: the report page must keep working if the CRM is down.
    """
    try:
        from services.ai_interview_bridge import sync_hr_decision
        sync_hr_decision(
            decision=decision,
            decided_by=_actor_label(actor) if actor is not None else None,
            invite_token=invite_token,
            interview_record_id=interview_record_id,
            candidate_email=candidate_email,
        )
    except Exception as exc:
        logging.getLogger(__name__).warning("CRM decision mirror failed: %s", exc)


def _enforce_crm_roles(request: Request, *allowed: str) -> None:
    """Apply Karnex CRM role-based access to a legacy Interview Platform endpoint.

    Mirrors the frontend RBAC (frontend admin-dashboard src/lib/rbac.ts). Raises
    HTTPException(403) when the caller has CRM roles that don't match `allowed`.
    Fails OPEN (no-op) when the CRM DB is unconfigured or the user has no CRM role
    yet, so existing HR logins keep working. Admin always passes. Call this AFTER
    the endpoint's own `_require_user(...)` check.
    """
    try:
        from crm_deps import enforce_roles
    except Exception:
        return
    enforce_roles(request, *allowed)


def _parse_cors_origins() -> list[str]:
    raw = (os.getenv("CORS_ALLOW_ORIGINS") or "").strip()
    if not raw:
        return list(CORS_DEFAULT_ORIGINS)
    parsed = [origin.strip() for origin in raw.split(",") if origin.strip()]
    return parsed or list(CORS_DEFAULT_ORIGINS)


def _runtime_core_checks() -> dict:
    return {
        "data_dir_exists": DATA_DIR.exists(),
        "data_file_parent_writable": DATA_FILE.parent.exists() and os.access(DATA_FILE.parent, os.W_OK),
        "hr_code_file_exists": HR_ACCESS_CODE_FILE.exists(),
        "database_connected": _auth_db_ping(),
        "database_backend": "postgresql" if str(AUTH_DB_TARGET).startswith(("postgresql://", "postgres://")) else "sqlite",
    }


def _auth_db_ping() -> bool:
    try:
        from auth_db import _connect_postgres, _is_postgres

        if _is_postgres(AUTH_DB_TARGET):
            with _connect_postgres(str(AUTH_DB_TARGET)) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
                    cur.fetchone()
            return True
        import sqlite3

        with sqlite3.connect(str(AUTH_DB_TARGET)) as conn:
            conn.execute("SELECT 1")
        return True
    except Exception:
        return False


def _openai_keys_status() -> dict:
    from openai_client import openai_key_configured

    return {
        "default": openai_key_configured("default"),
        "tts": openai_key_configured("tts"),
        "question": openai_key_configured("question"),
        "eval": openai_key_configured("eval"),
        "transcribe": openai_key_configured("transcribe"),
    }


def _runtime_config_checks() -> dict:
    report_code = _effective_report_code()
    return {
        "cors_restricted": "*" not in cors_origins,
        "report_code_configured": bool(report_code),
        "report_code_non_default": bool(report_code and report_code != REPORT_CODE),
        "openai_key_configured": any(
            openai_key_configured(p)
            for p in ("default", "question", "eval", "tts", "transcribe", "ats")
        ),
        "openai_keys": _openai_keys_status(),
    }


def _effective_report_code() -> str:
    file_code = ""
    try:
        if HR_ACCESS_CODE_FILE.exists():
            file_code = HR_ACCESS_CODE_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        file_code = ""
    env_code = (os.getenv("REPORT_CODE") or "").strip()
    return file_code or env_code or REPORT_CODE


def _provider_mode() -> str:
    return "openai"


def _write_report_code(new_code: str) -> None:
    HR_ACCESS_CODE_FILE.write_text((new_code or "").strip(), encoding="utf-8")


def _is_local_request(request: Request) -> bool:
    host = request.client.host if request.client else ""
    return host in {"127.0.0.1", "::1", "localhost"}


def _parse_scheduled_local(raw: str) -> datetime | None:
    text = (raw or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=IST)
    return parsed.astimezone(IST)


def _invite_access_state(record: dict) -> dict:
    scheduled_dt = _parse_scheduled_local(str(record.get("scheduled_at_local", "")))
    now = datetime.now(IST)
    if not scheduled_dt:
        return {"ok": True, "seconds_until_start": 0, "reason": ""}
    seconds_until = int((scheduled_dt - now).total_seconds())
    if seconds_until > 0:
        return {
            "ok": False,
            "seconds_until_start": seconds_until,
            "reason": "scheduled_wait",
            "starts_at_ist": scheduled_dt.isoformat(),
        }
    if now > (scheduled_dt + timedelta(hours=24)):
        return {
            "ok": False,
            "seconds_until_start": 0,
            "reason": "expired",
            "starts_at_ist": scheduled_dt.isoformat(),
        }
    return {
        "ok": True,
        "seconds_until_start": 0,
        "reason": "",
        "starts_at_ist": scheduled_dt.isoformat(),
    }


def _evaluate_and_store_report(session: dict) -> tuple[dict, dict, dict]:
    model = session.get("meta", {}).get("model", "gpt-4o-mini")
    meta = session.get("meta", {}) or {}
    jd_skills = meta.get("jd_skills", [])
    has_ai_key = openai_key_configured("eval")
    raw_q = list(session.get("questions") or [])
    raw_a = list(session.get("answers") or [])
    # Issue 2 (May 2026): drop the non-evaluated warmup turn from every
    # evaluation input. Scoring, communication scoring AND per-question
    # evaluations are computed over the filtered lists so the warmup never
    # appears in the HR report's score breakdown.
    full_q, full_a = filter_out_warmups(raw_q, raw_a, meta)
    # May 2026: score only answered turns — not the prefetched question pool.
    q_answered, a_answered, scope_meta = align_qa_to_answered_turns(full_q, full_a)
    q_eval, a_eval, _scr_meta = slice_qa_for_final_evaluation(q_answered, a_answered)
    try:
        result = evaluate_with_model_skill_based(
            q_eval, a_eval, jd_skills=jd_skills, model=model
        )
        result["evaluation_mode"] = "openai"
    except OpenAIError:
        result = evaluate_fallback_skill_based(
            jd_skills, a_eval, questions=q_eval
        )
        result["evaluation_mode"] = "fallback_no_openai" if not has_ai_key else "fallback_openai_error"
    except Exception:
        result = evaluate_fallback_skill_based(
            jd_skills, a_eval, questions=q_eval
        )
        result["evaluation_mode"] = "fallback_exception"

    # RMG can declare that a role does not genuinely require spoken
    # communication (template setting "Assess communication"). When it is off
    # the report covers technical performance only — so the separate
    # communication evaluation is skipped entirely rather than computed and
    # hidden. Computing it anyway would leave a score in the stored record that
    # some other view could surface later.
    assess_communication = bool(meta.get("communication_required", True))
    result["communication_required"] = assess_communication
    if assess_communication:
        result["communication_evaluation"] = evaluate_communication_skills(
            q_eval, a_eval, model=model
        )
    else:
        result["communication_evaluation"] = {}

    # The warmup was already removed by filter_out_warmups above, so q_answered/
    # a_answered contain ONLY technical turns. Passing `meta` (whose
    # warmup_indices/question_types still point at pre-filter position 0) would
    # make the scorer treat the FIRST technical question as the intro and drop it
    # from scoring. Pass None so every technical answer is scored.
    result = merge_per_question_eval_into_report(
        result,
        q_answered,
        a_answered,
        model=model,
        session_meta=None,
        assess_communication=assess_communication,
    )
    result["evaluation_scope"] = scope_meta
    result = apply_decimal_scores_to_report(result)
    result = _attach_boundary_question_to_report(session, result)
    result = attach_strengths_weaknesses_analysis(
        result,
        q_answered,
        a_answered,
        model=model,
    )

    evaluated_ist = _now_ist_parts()
    record = build_report_record(session, result, evaluated_ist)
    upsert_interview_record_snapshot(AUTH_DB_TARGET, record)
    _persist_hr_record_mirror(record)
    invalidate_hr_dashboard_cache()
    session["report_result"] = result
    session["report_generated_at_ist"] = evaluated_ist["ist_iso"]
    return result, evaluated_ist, record


def _progress_status_for_session(session: dict, status: str | None = None) -> str:
    if status:
        return status
    meta = session.get("meta", {}) or {}
    if meta.get("termination_reason") or session.get("terminated"):
        return "terminated"
    if session.get("submitted") or session.get("completed"):
        return "completed"
    if session.get("answers"):
        return "in_progress"
    return "started"


def _persist_interview_progress(session: dict, status: str | None = None, report_status: str | None = None, report_error: str = "") -> None:
    """Crash-safe progress checkpoint. Best-effort: interview flow must continue if persistence has a transient issue."""
    try:
        meta = session.get("meta", {}) or {}
        interview_id = str(meta.get("interview_id") or "").strip()
        if not interview_id:
            return
        now = _now_ist_parts()["ist_iso"]
        effective_status = _progress_status_for_session(session, status)
        final_report_status = report_status
        if final_report_status is None:
            final_report_status = "ready" if session.get("report_result") else ("pending" if effective_status in {"submitting", "completed", "terminated", "abandoned", "partially_completed"} else "")
        upsert_interview_progress(
            AUTH_DB_TARGET,
            {
                "interview_id": interview_id,
                "invite_token": str(meta.get("invite_token") or ""),
                "candidate_name": str((meta.get("candidate_profile") or {}).get("name") or ""),
                "candidate_email": str((meta.get("candidate_profile") or {}).get("email") or ""),
                "status": effective_status,
                "current_index": int(session.get("current") or 0),
                "questions": list(session.get("questions") or []),
                "answers": list(session.get("answers") or []),
                "meta": meta,
                "violations": list(meta.get("violations") or []),
                "payload": copy.deepcopy(session),
                "last_activity_at": now,
                "finalized_at": now if effective_status in {"completed", "terminated", "abandoned", "partially_completed", "recovered"} else "",
                "report_status": final_report_status,
                "report_error": report_error,
                "created_at_ist": str(meta.get("created_at_ist") or now),
                "updated_at_ist": now,
            },
        )
    except Exception as exc:
        logger.warning("interview.progress.persist_failed: %s", exc, exc_info=True)


def _session_from_progress(progress: dict | None) -> dict | None:
    if not progress:
        return None
    payload = progress.get("payload") if isinstance(progress.get("payload"), dict) else {}
    if payload.get("questions") is not None and payload.get("meta") is not None:
        sess = copy.deepcopy(payload)
    else:
        sess = {
            "meta": copy.deepcopy(progress.get("meta") or {}),
            "questions": list(progress.get("questions") or []),
            "answers": list(progress.get("answers") or []),
            "current": int(progress.get("current_index") or len(progress.get("answers") or [])),
            "completed": str(progress.get("status") or "").lower() in {"completed", "terminated", "abandoned", "partially_completed", "recovered"},
            "submitted": str(progress.get("report_status") or "").lower() == "ready",
        }
    sess.setdefault("meta", {})
    sess.setdefault("questions", [])
    sess.setdefault("answers", [])
    sess.setdefault("current", int(progress.get("current_index") or len(sess.get("answers") or [])))
    sess.setdefault("completed", False)
    sess.setdefault("submitted", False)
    return sess


def _fallback_report_result(session: dict, reason: str, final_status: str, error: str = "") -> dict:
    meta = session.get("meta", {}) or {}
    raw_q = list(session.get("questions") or [])
    raw_a = list(session.get("answers") or [])
    full_q, full_a = filter_out_warmups(raw_q, raw_a, meta)
    q_answered, a_answered, scope_meta = align_qa_to_answered_turns(full_q, full_a)
    try:
        result = evaluate_fallback_skill_based(meta.get("jd_skills", []), a_answered, questions=q_answered)
    except Exception:
        answered_count = len([a for a in a_answered if str(a or "").strip()])
        score = 0 if not q_answered else round((answered_count / max(1, len(q_answered))) * 40, 2)
        result = {
            "overall_score": score,
            "skills": {},
            "summary": "Partial interview report generated from saved answers because final AI evaluation was unavailable.",
            "recommendation": "Review Required",
        }
    result["evaluation_mode"] = str(result.get("evaluation_mode") or "fallback_recovery")
    result["report_type"] = final_status
    result["finalization_reason"] = reason
    result["finalization_error"] = str(error or "")[:1000]
    result["attempted_questions"] = len(q_answered)
    result["answered_questions"] = len([a for a in a_answered if str(a or "").strip() and str(a).strip().lower() != "skip"])
    result["skipped_questions"] = len([a for a in a_answered if str(a or "").strip().lower() == "skip"])
    result["integrity_violations"] = list((meta.get("violations") or []))[:50]
    result["evaluation_scope"] = scope_meta
    result = apply_decimal_scores_to_report(result)
    return _attach_boundary_question_to_report(session, result)


def _finalize_interview_snapshot(session: dict, reason: str = "completed", final_status: str = "completed") -> dict:
    """Idempotent report guarantee: always persists an interview_records row with report content."""
    meta = session.get("meta", {}) or {}
    interview_id = str(meta.get("interview_id") or "").strip()
    if not interview_id:
        meta["interview_id"] = str(uuid4())
        interview_id = meta["interview_id"]
    existing = get_interview_record_payload(AUTH_DB_TARGET, interview_id)
    if isinstance(existing, dict) and existing.get("report"):
        _persist_interview_progress(session, status=final_status, report_status="ready")
        return {"status": "submitted", "report_ready": True, "reused_report": True, "interview_id": interview_id}

    session["submitted"] = True
    session["completed"] = True
    meta["final_status"] = final_status
    meta["finalization_reason"] = reason
    _persist_interview_progress(session, status="submitting", report_status="generating")
    report_error = ""
    try:
        result, evaluated_ist, report_record = _evaluate_and_store_report(session)
        report_record["final_status"] = final_status
        report_record["finalization_reason"] = reason
        report_record["report_status"] = "ready"
        upsert_interview_record_snapshot(AUTH_DB_TARGET, report_record)
        _persist_hr_record_mirror(report_record)
    except Exception as exc:
        report_error = str(exc)
        evaluated_ist = _now_ist_parts()
        result = _fallback_report_result(session, reason=reason, final_status=final_status, error=report_error)
        report_record = build_report_record(session, result, evaluated_ist)
        report_record["report_status"] = "fallback"
        report_record["final_status"] = final_status
        upsert_interview_record_snapshot(AUTH_DB_TARGET, report_record)
        _persist_hr_record_mirror(report_record)
        invalidate_hr_dashboard_cache()

    invite_token = str(meta.get("invite_token") or "").strip()
    if invite_token:
        existing_schedule = get_schedule_by_token(AUTH_DB_TARGET, invite_token)
        prior = _parse_violations_log((existing_schedule or {}).get("violations_log"))
        current_violations = list(meta.get("violations") or [])
        merged = _merge_finalize_violations(current_violations, prior)
        violation_count = _count_integrity_violations(merged)
        schedule_status = "terminated" if final_status == "terminated" else "completed"
        update_schedule_field(
            AUTH_DB_TARGET,
            invite_token,
            session_status=schedule_status,
            interview_completed_at=evaluated_ist["ist_iso"],
            violation_count=violation_count,
            violations_log=json.dumps(merged, ensure_ascii=False),
        )
        invalidate_integrity_logs_cache()

    try:
        append_from_evaluation(meta.get("jd_skills", []), result, interview_id)
    except Exception:
        pass
    session["report_result"] = result
    session["report_generated_at_ist"] = evaluated_ist["ist_iso"]
    _persist_interview_progress(
        session,
        status=final_status if final_status else "completed",
        report_status="ready",
        report_error=report_error,
    )
    return {
        "status": "submitted",
        "report_ready": True,
        "generated_at_ist": evaluated_ist["ist_iso"],
        "interview_id": interview_id,
        "report_error": report_error,
        "final_status": final_status,
    }


def _persist_fast_final_report(session: dict, reason: str, final_status: str) -> dict:
    """Persist a report immediately so candidate submit is fast and crash-safe."""
    meta = session.get("meta", {}) or {}
    interview_id = str(meta.get("interview_id") or "").strip()
    if not interview_id:
        meta["interview_id"] = str(uuid4())
        interview_id = meta["interview_id"]
    existing = get_interview_record_payload(AUTH_DB_TARGET, interview_id)
    if isinstance(existing, dict) and existing.get("report"):
        _persist_interview_progress(session, status=final_status, report_status="ready")
        return {"status": "submitted", "report_ready": True, "reused_report": True, "interview_id": interview_id}

    session["submitted"] = True
    session["completed"] = True
    session["finalizing"] = True
    meta["final_status"] = final_status
    meta["finalization_reason"] = reason
    evaluated_ist = _now_ist_parts()
    result = _fallback_report_result(session, reason=reason, final_status=final_status, error="")
    result = _attach_boundary_question_to_report(session, result)
    result["evaluation_mode"] = "fast_fallback_pending_ai"
    result["report_upgrade_pending"] = True
    report_record = build_report_record(session, result, evaluated_ist)
    report_record["report_status"] = "ready_pending_ai"
    report_record["final_status"] = final_status
    report_record["finalization_reason"] = reason
    upsert_interview_record_snapshot(AUTH_DB_TARGET, report_record)
    _persist_hr_record_mirror(report_record)
    invalidate_hr_dashboard_cache()

    invite_token = str(meta.get("invite_token") or "").strip()
    if invite_token:
        existing_schedule = get_schedule_by_token(AUTH_DB_TARGET, invite_token)
        prior = _parse_violations_log((existing_schedule or {}).get("violations_log"))
        current_violations = list(meta.get("violations") or [])
        merged = _merge_finalize_violations(current_violations, prior)
        violation_count = _count_integrity_violations(merged)
        update_schedule_field(
            AUTH_DB_TARGET,
            invite_token,
            session_status="terminated" if final_status == "terminated" else "completed",
            interview_completed_at=evaluated_ist["ist_iso"],
            violation_count=violation_count,
            violations_log=json.dumps(merged, ensure_ascii=False),
        )
        invalidate_integrity_logs_cache()

    session["report_result"] = result
    session["report_generated_at_ist"] = evaluated_ist["ist_iso"]
    _persist_interview_progress(session, status=final_status, report_status="generating")
    logger.info(
        "[REPORT] Generation Completed",
        extra={"event": "report.generation.completed", "interview_id": interview_id, "fast_finalize": True},
    )
    return {
        "status": "submitted",
        "report_ready": False,
        "report_status": "generating",
        "generated_at_ist": evaluated_ist["ist_iso"],
        "interview_id": interview_id,
        "final_status": final_status,
        "fast_finalize": True,
        "background_report_upgrade": True,
    }


def _upgrade_interview_report_background(session_snapshot: dict, reason: str, final_status: str) -> None:
    """Best-effort upgrade from fast fallback report to the normal AI evaluation report."""
    try:
        meta = session_snapshot.get("meta", {}) or {}
        meta["final_status"] = final_status
        meta["finalization_reason"] = reason
        result, evaluated_ist, report_record = _evaluate_and_store_report(session_snapshot)
        report_record["final_status"] = final_status
        report_record["finalization_reason"] = reason
        report_record["report_status"] = "ready"
        upsert_interview_record_snapshot(AUTH_DB_TARGET, report_record)
        _persist_hr_record_mirror(report_record)
        _persist_interview_progress(session_snapshot, status=final_status, report_status="ready")
        append_from_evaluation(meta.get("jd_skills", []), result, str(meta.get("interview_id", "")))
        sk = _session_key_from_session(session_snapshot)
        sessions.pop(sk, None)
        logger.info(
            "interview.report.upgraded.background",
            extra={
                "event": "interview.report.upgraded.background",
                "interview_id": str(meta.get("interview_id", "")),
                "final_status": final_status,
            },
        )
    except Exception as exc:
        logger.warning("background report upgrade failed: %s", exc, exc_info=True)


def _parse_progress_activity(raw: str) -> datetime | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=IST)
    return dt.astimezone(IST)


def _should_recover_progress(row: dict, now: datetime) -> bool:
    if str(row.get("report_status") or "").strip().lower() == "ready":
        return False
    status = str(row.get("status") or "").strip().lower()
    if status in {"submitting", "completed", "terminated", "abandoned", "partially_completed", "recovered"}:
        return True
    answers = row.get("answers") if isinstance(row.get("answers"), list) else []
    last = _parse_progress_activity(str(row.get("last_activity_at") or row.get("updated_at_ist") or row.get("created_at_ist") or ""))
    idle_seconds = (now - last).total_seconds() if last else float("inf")
    recovery_idle_sec = max(30 * 60, min(45 * 60, int(os.getenv("INTERVIEW_RECOVERY_IDLE_MIN", "35") or "35") * 60))
    # Active/in-progress rows with recent activity are not recoverable yet.
    if status in {"started", "in_progress"} and last and idle_seconds < recovery_idle_sec:
        return False
    if answers and idle_seconds >= recovery_idle_sec:
        return True
    if status in {"started", "in_progress"} and idle_seconds >= 60 * 60:
        return True
    return False


def _recover_interviews_once(limit: int = 100) -> int:
    recovered = 0
    now = datetime.now(IST)
    try:
        rows = list_recoverable_interview_progress(AUTH_DB_TARGET, limit=limit)
    except Exception as exc:
        logger.warning("interview.recovery.scan_failed: %s", exc, exc_info=True)
        return 0
    for row in rows:
        try:
            if not _should_recover_progress(row, now):
                continue
            sess = _session_from_progress(row)
            if not sess:
                continue
            status = str(row.get("status") or "").strip().lower()
            answers = sess.get("answers") or []
            if status == "terminated":
                final_status = "terminated"
                reason = "terminated_recovery"
            elif answers:
                final_status = "recovered"
                reason = "auto_recovered_after_inactivity"
            else:
                final_status = "abandoned"
                reason = "abandoned_without_answers"
            out = _finalize_interview_snapshot(sess, reason=reason, final_status=final_status)
            token = str((sess.get("meta", {}) or {}).get("invite_token") or row.get("invite_token") or "").strip()
            if token:
                sessions.pop(f"inv:{token}", None)
            recovered += 1
            logger.info(
                "interview.recovery.finalized",
                extra={
                    "event": "interview.recovery.finalized",
                    "interview_id": row.get("interview_id", ""),
                    "invite_token": _invite_token_tag(token),
                    "final_status": final_status,
                    "report_ready": bool(out.get("report_ready")),
                },
            )
        except Exception as exc:
            logger.warning(
                "interview.recovery.row_failed: %s",
                exc,
                extra={"event": "interview.recovery.row_failed", "interview_id": row.get("interview_id", "")},
                exc_info=True,
            )
    return recovered


_RECOVERY_WORKER_STARTED = False


def _recovery_worker_loop() -> None:
    interval = max(30, min(600, int(os.getenv("INTERVIEW_RECOVERY_INTERVAL_SEC", "60") or "60")))
    while True:
        try:
            _recover_interviews_once(limit=100)
        except Exception as exc:
            logger.warning("interview.recovery.loop_failed: %s", exc, exc_info=True)
        time.sleep(interval)


app = FastAPI(title=APP_TITLE)

# Rate limiting must be wired up BEFORE any @_rl.limit(...) decorator below is
# evaluated: limit() resolves the limiter at decoration time, so calling this
# at the bottom of the module (as it used to be) left all per-route limits inert.
_rl.setup_rate_limit(app)


@app.on_event("startup")
def _start_daily_scheduler() -> None:
    """Run the daily jobs: timesheet-due reminders, PO-expiry milestone notices
    and recurring invoice drafts.

    Separate hook and separate thread from the outbox worker so neither can
    stall the other, and disabled on its own with SCHEDULER_WORKER=false (for a
    second app instance, or while debugging). Every message the jobs send is
    deduped by a milestone key, so even a double start cannot duplicate mail.
    """
    try:
        from services.scheduler import start_scheduler

        start_scheduler()
    except Exception as exc:  # never block boot on background jobs
        logger.warning("scheduler.worker_start_failed: %s", exc)


@app.on_event("startup")
def _start_email_outbox_worker() -> None:
    """Drain the notification email outbox in the background.

    Kept separate from the interview-recovery worker so an SMTP problem can
    never stall interview crash recovery, and so it can be turned off on its own
    with EMAIL_OUTBOX_WORKER=false (e.g. when a cron drains the table instead).
    """
    try:
        from services.email_outbox import start_outbox_worker

        start_outbox_worker()
    except Exception as exc:  # never block boot on the notifier
        logger.warning("email.outbox.worker_start_failed: %s", exc)


@app.on_event("startup")
def _start_interview_recovery_worker() -> None:
    global _RECOVERY_WORKER_STARTED
    if _is_production_env():
        if _auth_secret_is_default():
            raise RuntimeError(
                "Refusing to start in production with default AUTH_SECRET/REPORT_CODE. "
                "Set strong secrets in the environment."
            )
        if not (os.getenv("CORS_ALLOW_ORIGINS") or "").strip():
            logger.warning(
                "CORS_ALLOW_ORIGINS is unset in production — set explicit trusted origins.",
                extra={"event": "startup.cors_unset"},
            )
    try:
        workers = int(str(os.getenv("UVICORN_WORKERS") or "1").strip() or "1")
    except ValueError:
        workers = 1
    redis_url = (os.getenv("REDIS_URL") or "").strip()
    if workers > 1 and not redis_url:
        logger.warning(
            "UVICORN_WORKERS=%s without REDIS_URL — in-memory sessions/proctor state are not shared across workers. "
            "Use UVICORN_WORKERS=1 until Redis-backed sessions exist.",
            workers,
            extra={"event": "startup.multi_worker_warning", "workers": workers},
        )
    if _RECOVERY_WORKER_STARTED:
        return
    _RECOVERY_WORKER_STARTED = True
    t = threading.Thread(target=_recovery_worker_loop, daemon=True, name="interview-recovery")
    t.start()


@lru_cache(maxsize=1)
def _runtime_version() -> str:
    """
    Cheap, stable version string for cache busting (cached per process — avoids git subprocess per request).
    Prefers git short SHA when available; otherwise falls back to an app timestamp.
    """
    try:
        sha = (
            subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=str(Path(__file__).resolve().parent.parent))
            .decode("utf-8", "ignore")
            .strip()
        )
        if sha:
            return sha
    except Exception:
        pass
    try:
        return str(int(Path(__file__).stat().st_mtime))
    except Exception:
        return "unknown"


@app.get("/version")
def version():
    return {"version": _runtime_version(), "service": APP_TITLE}


DATA_FILE = HR_RECORDS_FILE

ensure_project_dirs()
migrate_legacy_data_files()
_auth_db_url_configured = bool((os.getenv("AUTH_DB_URL") or "").strip())
AUTH_DB_TARGET = _auth_db_target()
try:
    init_auth_db(AUTH_DB_TARGET)
except Exception as err:
    logger.error(
        "auth.db.init.failed",
        extra={"event": "auth.db.init.failed", "target": str(AUTH_DB_TARGET), "error": str(err)},
    )
    if _auth_db_url_configured:
        raise
    AUTH_DB_TARGET = KARNEX_DB_FILE
    init_auth_db(AUTH_DB_TARGET)
try:
    init_prompt_log_table(AUTH_DB_TARGET)
except Exception as err:
    logger.warning("prompt_log.table.init.failed", extra={"event": "prompt_log.table.init.failed", "error": str(err)})

try:
    bulk_import_interview_records(AUTH_DB_TARGET, load_hr_records(DATA_FILE))
except Exception as err:
    logger.warning("auth.db.import.records.failed", extra={"event": "auth.db.import.records.failed", "error": str(err)})

try:
    migrated = _migrate_job_configs_to_db_if_needed()
    if migrated:
        logger.info("job_templates.migrated", extra={"event": "job_templates.migrated", "rows_imported": migrated})
except Exception as err:
    logger.warning("job_templates.migration.failed", extra={"event": "job_templates.migration.failed", "error": str(err)})
try:
    imported = backfill_learning_from_records(load_hr_records(DATA_FILE))
    if imported:
        logger.info(
            "learning.backfill.completed",
            extra={"event": "learning.backfill.completed", "rows_imported": imported},
        )
except Exception as err:
    logger.warning("learning.backfill.failed", extra={"event": "learning.backfill.failed", "error": str(err)})
if not HR_ACCESS_CODE_FILE.exists():
    HR_ACCESS_CODE_FILE.write_text(_effective_report_code(), encoding="utf-8")

cors_origins = _parse_cors_origins()
cors_allow_regex = None
if not (os.getenv("CORS_ALLOW_ORIGINS") or "").strip():
    if _is_production_env():
        logger.warning(
            "CORS_ALLOW_ORIGINS not set — falling back to LAN regex is unsafe for production.",
            extra={"event": "cors.production_fallback"},
        )
    # Make dev/LAN usage IP-agnostic without needing to edit env/IP each run.
    # This is only used when CORS_ALLOW_ORIGINS is not explicitly set.
    cors_allow_regex = r"^https?://(localhost|127\.0\.0\.1|(\d{1,3}\.){3}\d{1,3})(:\d+)?$"
allow_credentials = "*" not in cors_origins and cors_allow_regex is None

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_origin_regex=cors_allow_regex,
    allow_credentials=allow_credentials,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(GZipMiddleware, minimum_size=900)


def _security_headers_enabled() -> bool:
    return str(os.getenv("SECURITY_HEADERS_ENABLED", "true")).strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


@app.middleware("http")
async def _security_headers(request: Request, call_next):
    response = await call_next(request)
    if not _security_headers_enabled():
        return response
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(self), microphone=(self), geolocation=()")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; media-src 'self' blob:; "
        "connect-src 'self' https://api.openai.com https://cdn.jsdelivr.net; frame-ancestors 'self'",
    )
    if request.url.scheme == "https" or str(os.getenv("HSTS_ENABLED", "")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response


@app.exception_handler(ValueError)
async def _handle_value_error(request: Request, exc: ValueError):
    return JSONResponse({"error": str(exc) or "Invalid input"}, status_code=400)


@app.exception_handler(RequestValidationError)
async def _handle_validation_error(request: Request, exc: RequestValidationError):
    from fastapi.encoders import jsonable_encoder

    errs = exc.errors() if hasattr(exc, "errors") else []
    # ctx may hold raw Exception instances — must be JSON-safe or Starlette 500s.
    safe = jsonable_encoder(errs)
    parts = []
    for row in safe if isinstance(safe, list) else []:
        if not isinstance(row, dict):
            continue
        loc = row.get("loc") or []
        loc_s = ".".join(str(p) for p in loc if p != "body")
        msg = row.get("msg") or "Invalid value"
        parts.append(f"{loc_s}: {msg}" if loc_s else str(msg))
    detail = "; ".join(parts) if parts else "Validation failed"
    return JSONResponse(
        {"error": "Validation failed", "detail": detail, "details": safe, "message": detail},
        status_code=422,
    )


@app.exception_handler(OpenAIError)
async def _handle_openai_error(request: Request, exc: OpenAIError):
    logger.warning("openai.error", extra={"event": "openai.error", "error": str(exc)})
    return JSONResponse(
        {"error": "AI service unavailable. Please retry shortly."},
        status_code=502,
    )


@app.exception_handler(Exception)
async def _handle_unexpected(request: Request, exc: Exception):
    logger.exception("unhandled.error", extra={"event": "unhandled.error", "path": str(request.url.path)})
    return JSONResponse({"error": "Internal server error"}, status_code=500)


@app.middleware("http")
async def _static_cache_headers(request: Request, call_next):
    """Long-cache hashed Vite assets; never cache HTML shells."""
    response = await call_next(request)
    path = request.url.path
    if "cache-control" in {k.lower() for k in response.headers.keys()}:
        return response
    if path.startswith("/admin/assets/") or path.startswith("/assets/"):
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    elif (
        path in ("/",)
        or path == "/admin"
        or path == "/admin/"
        or path.endswith("/index.html")
        or (path.endswith(".html") and not path.startswith("/admin/assets"))
    ):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, private"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


_auth_rate_lock = threading.Lock()
_auth_rate_hits: dict[str, deque[float]] = {}
_ANSWER_AUDIT_LOCK = threading.Lock()
_ANSWER_AUDIT_MAX = max(200, int((os.getenv("ANSWER_AUDIT_MAX") or "5000").strip() or "5000"))
_ANSWER_AUDIT: deque[dict] = deque(maxlen=_ANSWER_AUDIT_MAX)


def _allow_auth_rate_limit(client_ip: str, path: str) -> bool:
    raw = (os.getenv("AUTH_LOGIN_RATE_LIMIT") or "30").strip() or "30"
    try:
        limit = int(raw)
    except ValueError:
        limit = 30
    limit = max(8, min(limit, 120))
    key = f"{client_ip}:{path}"
    now = time.time()
    window = 60.0
    with _auth_rate_lock:
        dq = _auth_rate_hits.setdefault(key, deque())
        while dq and now - dq[0] > window:
            dq.popleft()
        if len(dq) >= limit:
            return False
        dq.append(now)
        return True


@app.middleware("http")
async def request_logger(request: Request, call_next):
    started = time.perf_counter()
    response = await call_next(request)
    duration_ms = round((time.perf_counter() - started) * 1000, 2)
    logger.info(
        "request.completed",
        extra={
            "event": "request.completed",
            "method": request.method,
            "path": request.url.path,
            "status_code": response.status_code,
            "duration_ms": duration_ms,
            "client": request.client.host if request.client else "unknown",
        },
    )
    return response


@app.middleware("http")
async def request_actor_scope(request: Request, call_next):
    """One actor holder per request so outgoing mail can be sent as the
    logged-in user (services/actor_context.py)."""
    from services.actor_context import begin_request
    begin_request()
    return await call_next(request)


@app.middleware("http")
async def auth_login_rate_limit(request: Request, call_next):
    if request.method == "POST" and request.url.path in ("/auth/login", "/auth/register"):
        ip = request.client.host if request.client else "unknown"
        if not _allow_auth_rate_limit(ip, request.url.path):
            return JSONResponse(
                {"detail": "Too many requests from this address. Wait a minute and try again."},
                status_code=429,
            )
    return await call_next(request)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    """Baseline browser hardening. Deliberately omits a strict script-src CSP so
    the SPA and interview portal keep working; frame-ancestors gives clickjacking
    protection equivalent to X-Frame-Options. HSTS is inert over plain HTTP and
    takes effect automatically once the app is served behind TLS (see docs)."""
    response = await call_next(request)
    headers = response.headers
    headers.setdefault("X-Content-Type-Options", "nosniff")
    headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    headers.setdefault("Permissions-Policy", "geolocation=(), payment=()")
    headers.setdefault("Content-Security-Policy", "frame-ancestors 'self'")
    headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response


@app.post("/setup")
async def setup(
    request: Request,
    jd: str = Form(""),
    cv: str = Form(""),
    jd_file: UploadFile | None = File(None),
    cv_file: UploadFile | None = File(None),
    difficulty: str = Form(...),
    num_q: int = Form(...),
    model: str = Form(""),
    custom_model: str = Form(""),
    safe_mode: str = Form("false"),
    followup_mode: str = Form("false"),
    interview_mode: str = Form(""),
    timing_mode: str = Form("count"),
    time_limit_sec: int = Form(0),
    mic_always_on: str = Form("false"),
    show_spoken_text: str = Form("false"),
    enable_transcript_input: str = Form(""),
    final_skills: str = Form(""),
    candidate_name: str = Form(""),
    candidate_experience: str = Form(""),
    candidate_email: str = Form(""),
    candidate_role: str = Form(""),
    jobId: str = Form(""),
):
    payload, auth_err = _require_user(request, {"hr", "manager", "admin"})
    if auth_err:
        return auth_err
    setup_session_key = _session_key_from_payload(payload)
    has_ai = openai_key_configured("question")

    selected_model = (custom_model or model).strip() or "gpt-4o-mini"
    safe_mode_on = str(safe_mode).strip().lower() in {"1", "true", "yes", "on"}
    followup_mode_on = _adaptive_followup_enabled() and str(followup_mode).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    timing_mode_val = str(timing_mode).strip().lower() or "count"
    if timing_mode_val not in {"count", "time"}:
        timing_mode_val = "count"
    time_limit_sec_val = max(0, min(int(time_limit_sec or 0), 6 * 60 * 60))
    mic_always_on_val = str(mic_always_on).strip().lower() in {"1", "true", "yes", "on"}
    transcript_toggle_raw = enable_transcript_input if str(enable_transcript_input).strip() else show_spoken_text
    show_spoken_text_val = str(transcript_toggle_raw).strip().lower() in {"1", "true", "yes", "on"}

    # Strict interview size cap requested by HR workflow.
    num_q = clamp_count_mode_questions(num_q)
    jd_text = jd.strip()
    cv_text = cv.strip()

    if jd_file is not None:
        jd_text = await _extract_text_from_upload(jd_file, selected_model, safe_mode_on)
        if jd_text.startswith("__ERR__"):
            return {"error": jd_text.replace("__ERR__", "", 1)}

    if cv_file is not None:
        cv_text = await _extract_text_from_upload(cv_file, selected_model, safe_mode_on)
        if cv_text.startswith("__ERR__"):
            return {"error": cv_text.replace("__ERR__", "", 1)}

    jd_skills = extract_jd_skills(jd_text)
    cv_skills = extract_cv_skills(cv_text)
    inferred_skills = infer_interview_skills(jd_text, cv_text)
    manual_skills = [s.strip().lower() for s in final_skills.split(",") if s.strip()]
    selected_skills = manual_skills or inferred_skills
    selected_skills = [s for s in selected_skills if s][:15]

    job_id_setup = str(jobId or "").strip()
    job_cfg_row = get_job_template(AUTH_DB_TARGET, job_id_setup) if job_id_setup else None
    setup_interview_mode = normalize_interview_mode(
        (interview_mode or "").strip()
        or (job_cfg_row or {}).get("interviewMode")
        or (job_cfg_row or {}).get("interview_mode")
    )
    job_title_for_session = str((job_cfg_row or {}).get("jobTitle") or "").strip()
    weights_for_suite: dict = {}
    if job_cfg_row and isinstance(job_cfg_row.get("weights"), dict):
        weights_for_suite = dict(job_cfg_row.get("weights") or {})
    effective_prompt_setup = _effective_template_prompt(job_cfg_row)
    prompt_skills_setup = _skills_from_template_prompt(effective_prompt_setup)
    if prompt_skills_setup:
        selected_skills = prompt_skills_setup[:15]
    template_custom_setup, validation_skills_setup = _template_generation_options_from_job(
        job_cfg_row,
        effective=effective_prompt_setup,
        form_skills=selected_skills,
    )
    jd_plain = jd_text
    jd_for_questions = _jd_with_intelligence_suite(jd_plain, weights_for_suite, role_fallback=job_title_for_session)
    seniority_tpl = str(weights_for_suite.get("intelligenceSeniority") or "").strip()
    experience_for_model = str(candidate_experience or "").strip()
    if seniority_tpl:
        experience_for_model = f"{seniority_tpl}; {experience_for_model}".strip("; ")

    if not manual_skills:
        return {
            "error": (
                "Final Skills are required. Please use 'Extract Skills' or enter Final Skills manually "
                "before starting the interview."
            )
        }

    if not selected_skills and not jd_text and not cv_text:
        return {
            "error": (
                "Provide JD/CV (text or file) or enter Final Skills manually. "
                "At least one source is required."
            )
        }

    warning = ""
    coach_hints_text()
    num_q = clamp_count_mode_questions(num_q or 1)
    pool_q = pool_questions_for_timing(num_q, timing_mode_val, time_limit_sec=time_limit_sec_val)
    setup_domains: list[str] = []
    raw_setup_cats = weights_for_suite.get("questionCategories")
    if isinstance(raw_setup_cats, list):
        setup_domains = _resolve_domain_titles([str(cid).strip() for cid in raw_setup_cats if str(cid).strip()])

    interview_id = str(uuid4())
    created_ist = _now_ist_parts()
    question_seed = make_question_session_seed(
        str(candidate_email or "").strip().lower(),
        interview_id,
        str(candidate_name or "").strip().lower(),
        job_id_setup,
    )

    qt_setup = _coerce_question_type((job_cfg_row or {}).get("questionType"))
    manual_list_setup = _normalized_manual_questions_for_job((job_cfg_row or {}).get("manualQuestions"))
    is_manual_interview = bool(job_id_setup and job_cfg_row and qt_setup == "manual")

    used_setup_saved_preview = False
    if is_manual_interview:
        if not manual_list_setup:
            return {
                "error": (
                    "This job template is set to Manual Questions, but no questions are saved. "
                    "Edit the template, add at least one non-empty line under Manual Interview Questions, then save."
                )
            }
        questions = list(manual_list_setup)
        # See the invite path above — sequential order is now a template choice.
        if should_shuffle_manual_questions(weights_for_suite):
            questions = _shuffle_manual_questions_for_session(
                questions,
                interview_id,
                job_id_setup,
                str(candidate_email or "").strip().lower(),
                str(candidate_name or "").strip().lower(),
            )
        setup_timing = str((job_cfg_row or {}).get("timingMode") or (job_cfg_row or {}).get("timing_mode") or timing_mode_val or "count").strip().lower()
        if setup_timing == "count":
            ask_n = clamp_count_mode_questions(num_q or (job_cfg_row or {}).get("numQ") or (job_cfg_row or {}).get("num_q") or 5)
            if len(questions) > ask_n:
                questions = questions[:ask_n]
        pool_q = len(questions)
        num_q = len(questions)
        used_setup_saved_preview = True
        warning = ""
    else:
        setup_preview = weights_for_suite.get("previewQuestions")
        setup_preview_list: list[str] = []
        if isinstance(setup_preview, list):
            setup_preview_list = [str(q).strip() for q in setup_preview if str(q).strip()]

        used_setup_saved_preview = bool(setup_preview_list)
        avoid_hist_setup = _build_question_avoid_history(job_cfg_row, weights_for_suite)
        if setup_preview_list and has_ai and not safe_mode_on:
            setup_mode = setup_interview_mode
            setup_role = job_title_for_session or str(weights_for_suite.get("intelligenceTargetRole") or "").strip()
            setup_stack = str(weights_for_suite.get("intelligenceTechStack") or "").strip()
            questions = _generate_interview_questions(
                interview_mode=setup_mode,
                jd_text=jd_for_questions,
                cv_text=cv_text,
                difficulty=difficulty,
                n=pool_q,
                model=selected_model,
                skills=selected_skills,
                coach_hints="",
                experience=experience_for_model,
                domain_categories=setup_domains or None,
                role=setup_role,
                tech_stack=setup_stack,
                template_prompt=effective_prompt_setup,
                variety_seed=question_seed,
                avoid_history=avoid_hist_setup + setup_preview_list,
                template_custom=template_custom_setup,
                validation_skills=validation_skills_setup,
            )
        elif setup_preview_list:
            questions = prepare_unique_question_sequence(
                setup_preview_list,
                seed=question_seed,
                asked_questions=avoid_hist_setup,
                limit=pool_q,
            )
        elif has_ai and not safe_mode_on:
            setup_mode = setup_interview_mode
            setup_role = job_title_for_session or str(weights_for_suite.get("intelligenceTargetRole") or "").strip()
            setup_stack = str(weights_for_suite.get("intelligenceTechStack") or "").strip()
            questions = _generate_interview_questions(
                interview_mode=setup_mode,
                jd_text=jd_for_questions,
                cv_text=cv_text,
                difficulty=difficulty,
                n=pool_q,
                model=selected_model,
                skills=selected_skills,
                coach_hints="",
                experience=experience_for_model,
                domain_categories=setup_domains or None,
                role=setup_role,
                tech_stack=setup_stack,
                template_prompt=effective_prompt_setup,
                variety_seed=question_seed,
                avoid_history=avoid_hist_setup,
                template_custom=template_custom_setup,
                validation_skills=validation_skills_setup,
            )
        elif template_custom_setup:
            questions = []
        else:
            questions = generate_questions_fallback(
                jd_for_questions,
                cv_text,
                difficulty,
                pool_q,
                required_skills=selected_skills,
            )
            warning = (
                "Safe mode is ON. Using stable fallback generation aligned to final skills."
                if safe_mode_on
                else "No API key detected. Using fallback generation aligned to final skills."
            )

        if used_setup_saved_preview:
            questions = [q for q in questions if q][:pool_q]
        else:
            questions = [q for q in questions if q and len(q) <= 180]
            if selected_skills and questions:
                missing = [sk for sk in selected_skills if not any(question_matches_skill(q, sk) for q in questions)]
                for idx, sk in enumerate(missing):
                    replacement = generate_questions_fallback(jd_for_questions, cv_text, difficulty, 1, required_skills=[sk])[0]
                    rep_idx = (len(questions) - 1 - idx) % max(len(questions), 1)
                    questions[rep_idx] = replacement
    if not questions:
        return {"error": "No questions generated. Please retry with better JD/CV content."}

    questions = prepare_unique_question_sequence(questions, seed=question_seed, limit=pool_q)
    from utils.question_uniqueness import dedupe_question_list_semantic, record_generated_questions_batch

    questions = dedupe_question_list_semantic(questions)
    if not questions:
        return {"error": "No unique questions generated. Please retry with better JD/CV content."}

    # Issue 2 (May 2026): inject the introduce-yourself warmup at index 0.
    # The /setup HR-direct path mirrors the invite flow so both entrypoints
    # produce a consistent "warmup first" candidate experience.
    questions, warmup_indices = inject_warmup(questions)
    questions = trim_questions_for_count_mode(
        questions,
        num_q,
        timing_mode_val,
        warmup_count=len(warmup_indices or []),
    )

    extracted_profile = extract_candidate_profile(cv_text)
    candidate_profile = _build_candidate_profile(
        extracted_profile,
        candidate_name=candidate_name,
        candidate_experience=candidate_experience,
        candidate_email=candidate_email,
        candidate_role=candidate_role,
    )
    if job_cfg_row:
        jt_align = str((job_cfg_row or {}).get("jobTitle") or "").strip()
        if jt_align:
            candidate_profile["role_hint"] = jt_align
    if not candidate_profile.get("name"):
        return {"error": "Candidate name is required."}
    raw_qc_setup = weights_for_suite.get("questionCategories")
    question_categories_setup = [str(x).strip() for x in raw_qc_setup] if isinstance(raw_qc_setup, list) else []
    sessions[setup_session_key] = {
        "meta": {
            "interview_id": interview_id,
            "hr_username": str((payload or {}).get("sub") or "").strip(),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "created_at_ist": created_ist["ist_iso"],
            "created_date_ist": created_ist["ist_date"],
            "created_time_ist": created_ist["ist_time"],
            "difficulty": difficulty,
            "num_q": num_q,
            "model": selected_model,
            "generation_mode": ("manual" if is_manual_interview else ("ai" if not warning else "fallback")),
            "question_source": "manual" if is_manual_interview else "dynamic",
            "safe_mode": safe_mode_on,
            "candidate_profile": candidate_profile,
            "candidate_experience": candidate_experience,
            "jd_skills": selected_skills,
            "jd_skills_detected": jd_skills,
            "cv_skills_detected": cv_skills,
            "jd_text": jd_for_questions,
            "jd_text_plain": jd_plain,
            "question_categories": question_categories_setup,
            "intelligence_target_role": str(weights_for_suite.get("intelligenceTargetRole") or "").strip(),
            "intelligence_seniority": seniority_tpl,
            "intelligence_tech_stack": str(weights_for_suite.get("intelligenceTechStack") or "").strip(),
            "followup_mode": followup_mode_on,
            "interview_mode": setup_interview_mode,
            "interview_mode_label": to_display_label(setup_interview_mode),
            "timing_mode": timing_mode_val,
            "time_limit_sec": time_limit_sec_val,
            "mic_always_on": mic_always_on_val,
            "show_spoken_text": show_spoken_text_val,
            "enable_transcript_input": show_spoken_text_val,
            "session_difficulty": str(difficulty).strip().lower()
            if str(difficulty).strip().lower() in ("easy", "medium", "hard")
            else "medium",
            "followups_added": 0,
            "max_followups": 0
            if (is_manual_interview or not followup_mode_on)
            else max(0, pool_q - len(selected_skills)),
            "job_id": job_id_setup,
            "job_title": job_title_for_session,
            "question_seed": question_seed,
            "asked_questions": [],
            "pool_generated": len(questions),
            "warmup_indices": warmup_indices,
            "template_prompt": effective_prompt_setup,
            "adaptive_next_question": bool((weights_for_suite or {}).get("adaptiveNextQuestion", False)),
        },
        "questions": questions,
        "answers": [],
        "current": 0,
        "completed": False,
        "submitted": False,
    }
    stamp_time_warning_settings(sessions[setup_session_key]["meta"], weights_for_suite)
    stamp_auto_advance_settings(sessions[setup_session_key]["meta"], weights_for_suite)
    stamp_template_settings(sessions[setup_session_key]["meta"], weights_for_suite)
    stamp_introduction_question_types(sessions[setup_session_key]["meta"], warmup_indices)
    record_generated_questions_batch(
        sessions[setup_session_key],
        questions,
        source="manual" if is_manual_interview else "dynamic",
    )
    _persist_interview_progress(sessions[setup_session_key], status="started")
    logger.info(
        "interview.setup.completed",
        extra={
            "event": "interview.setup.completed",
            "interview_id": interview_id,
            "candidate_name": candidate_profile.get("name", "Candidate"),
        },
    )

    suggested_final = merge_unique_skills(jd_skills, cv_skills, inferred_skills, selected_skills)

    return {
        "session_id": setup_session_key,
        "interview_id": interview_id,
        "created_at_ist": created_ist["ist_iso"],
        "created_date_ist": created_ist["ist_date"],
        "created_time_ist": created_ist["ist_time"],
        "question_count": len(questions),
        "warning": warning,
        "candidate_profile": candidate_profile,
        "jd_skills": selected_skills,
        "jd_skills_detected": jd_skills,
        "cv_skills_detected": cv_skills,
        "inferred_skills": inferred_skills,
        "suggested_final_skills": suggested_final,
        "timing_mode": timing_mode_val,
        "time_limit_sec": time_limit_sec_val,
        "mic_always_on": mic_always_on_val,
        "show_spoken_text": show_spoken_text_val,
        "enable_transcript_input": show_spoken_text_val,
    }


@app.post("/extract-skills")
async def extract_skills(
    request: Request,
    jd: str = Form(""),
    cv: str = Form(""),
    jd_file: UploadFile | None = File(None),
    cv_file: UploadFile | None = File(None),
    model: str = Form("gpt-4o-mini"),
    custom_model: str = Form(""),
    safe_mode: str = Form("false"),
    candidate_name: str = Form(""),
    candidate_experience: str = Form(""),
    candidate_email: str = Form(""),
    candidate_role: str = Form(""),
):
    _, auth_err = _require_user(request, {"hr"})
    if auth_err:
        return auth_err
    selected_model = (custom_model or model).strip() or "gpt-4o-mini"
    safe_mode_on = str(safe_mode).strip().lower() in {"1", "true", "yes", "on"}

    jd_text = jd.strip()
    cv_text = cv.strip()

    if jd_file is not None:
        jd_text = await _extract_text_from_upload(jd_file, selected_model, safe_mode_on)
        if jd_text.startswith("__ERR__"):
            return {"error": jd_text.replace("__ERR__", "", 1)}

    if cv_file is not None:
        cv_text = await _extract_text_from_upload(cv_file, selected_model, safe_mode_on)
        if cv_text.startswith("__ERR__"):
            return {"error": cv_text.replace("__ERR__", "", 1)}

    if not jd_text and not cv_text:
        return {"error": "Upload or paste JD/CV first to extract skills."}

    jd_skills = extract_jd_skills(jd_text) if jd_text else []
    cv_skills = extract_cv_skills(cv_text) if cv_text else []
    inferred_skills = infer_interview_skills(jd_text, cv_text)
    extracted_profile = extract_candidate_profile(cv_text) if cv_text else {}
    candidate_profile = _build_candidate_profile(
        extracted_profile,
        candidate_name=candidate_name,
        candidate_experience=candidate_experience,
        candidate_email=candidate_email,
        candidate_role=candidate_role,
    )

    suggested_final = merge_unique_skills(jd_skills, cv_skills, inferred_skills)

    return {
        "jd_skills_detected": jd_skills,
        "cv_skills_detected": cv_skills,
        "inferred_skills": inferred_skills,
        "suggested_final_skills": suggested_final,
        "candidate_profile": candidate_profile,
        # Raw extracted text (Aug 2026): the template form's JD-upload field
        # feeds this straight into jdText so dynamic question generation runs
        # off the uploaded document.
        "jd_text_extracted": jd_text,
        "cv_text_extracted": cv_text,
    }


@app.get("/next")
def next_question(request: Request):
    payload, auth_err = _require_user(request, {"hr", "candidate"})
    if auth_err:
        return auth_err
    sk = _session_key_from_payload(payload)
    s = sessions.get(sk)
    if not s:
        invite_token_from_token = str((payload or {}).get("invite_token") or "").strip()
        recovered = get_interview_progress_by_invite(AUTH_DB_TARGET, invite_token_from_token) if invite_token_from_token else None
        s = _session_from_progress(recovered)
        if s:
            sessions[sk] = s
            logger.info(
                "[SESSION] Existing Session Reused",
                extra={"event": "session.reused.next", "invite_token": _invite_token_tag(invite_token_from_token)},
            )
    if not s:
        return {"error": "No active session. Run setup first."}
    if s.get("finalizing"):
        return {"message": "Interview completed"}
    meta = s.get("meta", {}) or {}
    _persist_interview_progress(s, status=_progress_status_for_session(s))
    invite_token = str(meta.get("invite_token") or "").strip()
    if invite_token and not meta.get("startup_first_next_logged"):
        login_started_raw = str(meta.get("startup_login_accepted_at_utc") or "").strip()
        login_to_first_next_ms = 0
        login_latency_ms = int(meta.get("startup_login_latency_ms") or 0)
        if login_started_raw:
            try:
                login_dt = datetime.fromisoformat(login_started_raw)
                if login_dt.tzinfo is None:
                    login_dt = login_dt.replace(tzinfo=timezone.utc)
                login_to_first_next_ms = int((datetime.now(timezone.utc) - login_dt).total_seconds() * 1000)
            except Exception:
                login_to_first_next_ms = 0
        startup_total_ms = max(0, login_latency_ms) + max(0, login_to_first_next_ms)
        logger.info(
            "interview.invite.first_next",
            extra={
                "event": "interview.invite.first_next",
                "invite_token": _invite_token_tag(invite_token),
                "login_latency_ms": login_latency_ms,
                "login_to_first_next_ms": max(0, login_to_first_next_ms),
                "startup_total_ms": startup_total_ms,
                "prewarm_status": str(meta.get("startup_prewarm_status") or ""),
                "prewarm_latency_ms": int(meta.get("startup_prewarm_latency_ms") or 0),
                "question_index": int(s.get("current") or 0),
            },
        )
        meta["startup_first_next_logged"] = True

    return next_question_payload(s)


@app.post("/candidate/transcribe")
@_rl.limit("30/minute")
async def transcribe_candidate_audio(
    request: Request,
    audio_file: UploadFile | None = File(None),
):
    _, auth_err = _require_user(request, {"hr", "candidate"})
    if auth_err:
        return auth_err
    if audio_file is None:
        return {"error": "Audio file is required."}
    raw = await audio_file.read()
    if not raw:
        return {"error": "Audio payload is empty."}
    if len(raw) < 400:
        return {"error": "Recording was too short. Speak a bit longer, then stop the mic."}
    try:
        model_name = (os.getenv("OPENAI_TRANSCRIBE_MODEL") or "gpt-4o-mini-transcribe").strip()
        text = await run_in_threadpool(
            transcribe_speech_bytes,
            raw,
            audio_file.filename or "candidate-response.webm",
            audio_file.content_type or "audio/webm",
            model_name,
        )
        if not text:
            return {"error": "No speech detected. Please speak clearly and retry."}
        return {"text": text}
    except OpenAIError:
        return {"error": "Transcription service unavailable. Please retry in a moment."}
    except Exception:
        return {"error": "Transcription failed. Please retry."}


@app.post("/candidate/validate-speech")
async def validate_candidate_speech(request: Request):
    """Validate client VAD metadata before auto-skip (blocks skip when speech detected)."""
    _, auth_err = _require_user(request, {"hr", "candidate"})
    if auth_err:
        return auth_err
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    meta = body.get("auto_advance_meta") if isinstance(body.get("auto_advance_meta"), dict) else body
    action = str(body.get("action") or "skip").strip().lower()
    speech_detected = has_human_speech_evidence(meta if isinstance(meta, dict) else {})
    if action == "skip":
        allowed, reason = skip_allowed_by_speech_evidence(meta if isinstance(meta, dict) else {})
        logger.info(
            "[VAD] validate-speech",
            extra={
                "event": "vad.validate_speech",
                "action": action,
                "allow_skip": allowed,
                "speech_detected": speech_detected,
                "reason": reason or ("ok" if allowed else "speech_detected"),
            },
        )
        return {
            "allow_skip": allowed,
            "speech_detected": speech_detected,
            "reason": reason or ("ok" if allowed else "speech_detected"),
        }
    return {"speech_detected": speech_detected, "allow_skip": not speech_detected}


@app.post("/candidate/tts")
@_rl.limit("40/minute")
async def candidate_tts(
    request: Request,
    text: str = Form(""),
):
    _, auth_err = _require_user(request, {"hr", "candidate"})
    if auth_err:
        return auth_err
    payload = " ".join((text or "").split()).strip()
    if not payload:
        return {"error": "Text is required."}
    if len(payload) > 3800:
        payload = payload[:3797].rsplit(" ", 1)[0] + "…"
    from services.tts_prewarm import get_cached, tts_voice_and_model

    tts_voice, tts_model = tts_voice_and_model()

    # Serve a prefetched clip instantly when this question was warmed while the
    # candidate was answering the previous one. This is the difference between
    # "submit, wait, hear the next question" and "submit, hear the next question".
    cached = get_cached(payload, tts_voice, tts_model)
    if cached is not None:
        return Response(content=cached, media_type="audio/mpeg",
                        headers={"X-Karnex-TTS": "cache"})

    # Stream rather than buffer. `.read()` waited for the whole MP3 before a
    # single byte reached the browser, which put a 2-3s silence in front of
    # every question. Chunks let playback start while synthesis continues.
    try:
        stream = await run_in_threadpool(_open_tts_stream, payload, tts_voice, tts_model)
    except OpenAIError:
        return {"error": "Voice service unavailable."}
    except Exception:
        return {"error": "Voice synthesis failed."}
    if stream is None:
        return {"error": "Voice synthesis failed."}

    async def _relay():
        try:
            for chunk in stream:
                yield chunk
        except Exception:
            # The audio is already partly delivered; a mid-stream failure has to
            # end the response rather than turn into a JSON error the <audio>
            # element cannot understand.
            logging.getLogger(__name__).warning("TTS stream aborted mid-flight")
            return

    return StreamingResponse(
        _relay(),
        media_type="audio/mpeg",
        headers={"Cache-Control": "no-store", "X-Karnex-TTS": "stream"},
    )


def _open_tts_stream(payload: str, voice: str, model: str):
    """Start the OpenAI TTS stream on a worker thread and hand back the iterator."""
    from ai import stream_speech_bytes

    iterator = stream_speech_bytes(payload, voice, model)
    try:
        first = next(iterator)
    except StopIteration:
        return None

    def _chain():
        yield first
        yield from iterator

    return _chain()


@app.post("/candidate/tts/prewarm")
@_rl.limit("40/minute")
async def candidate_tts_prewarm(request: Request, text: str = Form("")):
    """Synthesise a question's audio ahead of time and hold it in memory.

    The server already warms the next question automatically when it serves the
    current one (candidate/service.py). This endpoint exists so the client can
    warm a question the server could not predict — chiefly an adaptive follow-up,
    whose text is only decided once the previous answer has been read.

    Returns immediately; synthesis happens on a worker thread. Failures are
    silent by design: a missed prefetch just means the normal streaming path
    runs and the candidate waits the usual amount.
    """
    _, auth_err = _require_user(request, {"hr", "candidate"})
    if auth_err:
        return auth_err
    from services.tts_prewarm import get_cached, normalize_tts_text, prewarm_tts, tts_voice_and_model

    payload = normalize_tts_text(text)
    if not payload:
        return {"status": "skipped"}
    voice, model = tts_voice_and_model()
    if get_cached(payload, voice, model) is not None:
        return {"status": "cached"}
    return {"status": "warming" if prewarm_tts(payload) else "busy"}


def _replace_question_slot(session: dict, index: int, new_question: str) -> None:
    """Swap a pending question for an adaptive follow-up, keeping TTS honest.

    The question at `index` may already have been synthesised and cached by the
    prefetcher. Serving that cached audio afterwards would speak a question that
    is no longer in the script — the candidate would hear one question and see
    another. So the old audio is dropped and the replacement warmed instead.
    """
    try:
        questions = session.get("questions") or []
        if not (0 <= index < len(questions)):
            return
        old_question = str(questions[index] or "")
        questions[index] = new_question

        from services.tts_prewarm import invalidate, prewarm_tts

        if old_question and old_question != new_question:
            invalidate(old_question)
        # Warm the replacement now — it is about to be asked.
        prewarm_tts(new_question)
    except Exception:
        # Never let cache bookkeeping break the interview: at worst the
        # candidate waits the normal synthesis time.
        try:
            session["questions"][index] = new_question
        except Exception:
            pass


def _schedule_turn_evaluation(session: dict, previous_question: str, answer_text: str) -> None:
    """Run per-turn OpenAI evaluation off the /answer critical path."""

    def _run() -> None:
        try:
            _apply_turn_evaluation(session, previous_question, answer_text)
        except Exception:
            pass

    threading.Thread(target=_run, daemon=True).start()


def _apply_turn_evaluation(session: dict, previous_question: str, answer_text: str) -> None:
    sk = _session_key_from_session(session)
    with session_lock(sk):
        meta = session.get("meta", {})
        if meta.get("safe_mode", True):
            return
        if not openai_key_configured("eval"):
            return
        jd_skills = meta.get("jd_skills", []) or []
        focus = detect_skill_from_question(previous_question, jd_skills) or (jd_skills[0] if jd_skills else "technical")
        cur = str(meta.get("session_difficulty") or meta.get("difficulty", "medium")).lower()
        if cur not in ("easy", "medium", "hard"):
            cur = "medium"
        try:
            ev = evaluate_turn_with_model(
                previous_question,
                answer_text,
                focus,
                cur,
                model=meta.get("model", "gpt-4o-mini"),
                assess_communication=bool(meta.get("communication_required", True)),
            )
        except Exception:
            return
        if not ev:
            return
        meta["last_turn_score"] = ev.get("score")
        meta["last_turn_feedback"] = (ev.get("feedback") or "")[:500]
        meta["last_turn_reason"] = (ev.get("reason") or "")[:300]
        nd = str(ev.get("next_difficulty", "")).lower().strip()
        if nd in ("easy", "medium", "hard"):
            meta["session_difficulty"] = nd


def _expand_time_mode_pool(session: dict) -> None:
    meta = session.get("meta", {})
    if str(meta.get("question_source") or "") == "manual":
        return
    if str(meta.get("timing_mode") or "") != "time":
        return
    if not openai_key_configured("question") or meta.get("safe_mode", True):
        return
    qs = list(session.get("questions") or [])
    cur = int(session.get("current", 0))
    max_pool = max(20, min(60, int(os.getenv("TIME_MODE_POOL_MAX", "50") or "50")))
    if len(qs) >= max_pool:
        return
    if len(qs) - cur > 8:
        return
    need = min(10, max_pool + 2 - len(qs))
    if need <= 0:
        return
    avoid = list(qs)
    avoid_hist = build_question_avoid_history(
        global_recent=recently_asked_questions(80),
        session_asked=meta.get("asked_questions") or [],
    )
    avoid.extend(avoid_hist)
    lvl = str(meta.get("session_difficulty") or meta.get("difficulty", "medium"))
    try:
        more = _generate_interview_questions(
            interview_mode=str(meta.get("interview_mode") or "technical"),
            jd_text=meta.get("jd_text", "") or "",
            cv_text="",
            difficulty=lvl,
            n=need,
            model=meta.get("model", "gpt-4o-mini"),
            skills=meta.get("jd_skills", []),
            coach_hints=coach_hints_text(),
            experience=str(meta.get("candidate_experience", "")),
            role=str(meta.get("job_title") or ""),
            tech_stack=str(meta.get("intelligence_tech_stack") or ""),
            template_prompt=str(meta.get("template_prompt") or ""),
            avoid_history=avoid,
            variety_seed=str(meta.get("question_seed") or ""),
        )
    except Exception:
        more = []
    for q in more or []:
        t = (q or "").strip()
        if not t or question_too_similar(t, avoid):
            continue
        avoid.append(t)
        qs.append(t)
    session["questions"] = qs


def _record_skipped_turn(
    session: dict,
    *,
    question_index: int,
    question_text: str,
    reason: str = "Candidate skipped manually",
) -> None:
    """Persist structured skip metadata on the session for reports and audits."""
    meta = session.setdefault("meta", {})
    turns = meta.setdefault("skipped_turns", [])
    if not isinstance(turns, list):
        turns = []
        meta["skipped_turns"] = turns
    now_ts = time.time()
    turns.append(
        {
            "question_index": int(question_index) + 1,
            "question_number": int(question_index) + 1,
            "question_text": str(question_text or "")[:900],
            "status": "SKIPPED",
            "reason": str(reason or "Candidate skipped manually")[:240],
            "timestamp_utc": datetime.fromtimestamp(now_ts, tz=timezone.utc).isoformat(),
            "ts": now_ts,
        }
    )


def _append_answer_audit(
    *,
    session_key: str,
    current_index: int,
    action: str,
    answer_text: str,
    is_skipped_answer: bool,
    status: str,
    reason: str = "",
) -> None:
    now_ts = time.time()
    item = {
        "ts": now_ts,
        "at_utc": datetime.fromtimestamp(now_ts, tz=timezone.utc).isoformat(),
        "session_key": str(session_key or ""),
        "current_index": int(current_index or 0),
        "action": str(action or ""),
        "answer_len": len(str(answer_text or "")),
        "answer_preview": str(answer_text or "")[:120],
        "is_skipped_answer": bool(is_skipped_answer),
        "status": str(status or ""),
        "reason": str(reason or ""),
    }
    with _ANSWER_AUDIT_LOCK:
        _ANSWER_AUDIT.append(item)


@app.post("/answer")
@_rl.limit("60/minute")
def answer(
    request: Request,
    ans: str = Form(""),
    action: str = Form("send"),
    skip_reason: str = Form(""),
    auto_advance_meta: str = Form(""),
):
    payload, auth_err = _require_user(request, {"hr", "candidate"})
    if auth_err:
        return auth_err
    device_err = _enforce_invite_device_binding(request, payload)
    if device_err:
        return device_err
    sk = _session_key_from_payload(payload)
    with session_lock(sk):
        s = sessions.get(sk)
        if not s:
            invite_token_from_token = str((payload or {}).get("invite_token") or "").strip()
            recovered = get_interview_progress_by_invite(AUTH_DB_TARGET, invite_token_from_token) if invite_token_from_token else None
            s = _session_from_progress(recovered)
            if s:
                sessions[sk] = s
                logger.info(
                    "[SESSION] Existing Session Reused",
                    extra={"event": "session.reused.answer", "invite_token": _invite_token_tag(invite_token_from_token)},
                )
        if not s:
            return {"error": "No active session. Run setup first."}
        if s.get("finalizing") or s.get("completed"):
            return {"error": "Interview already completed."}
        if int(s.get("current", 0) or 0) >= len(s.get("questions") or []):
            s["completed"] = True
            _persist_interview_progress(s, status="completed")
            return {"status": "completed", "answered": len(s.get("answers") or [])}

        turn_index = int(s.get("current", 0) or 0)
        answers = s.get("answers") or []
        if len(answers) > turn_index:
            existing_answer = answers[turn_index]
            is_skipped_existing = str(existing_answer or "").strip().lower() in {"skip", "skipped", "[skipped]"}
            return _build_answer_response(s, is_skipped_answer=is_skipped_existing)

        action_clean = str(action or "send").strip().lower() or "send"
        if action_clean not in {"send", "skip"}:
            action_clean = "send"
        ans_clean = str(ans or "").strip()
        if action_clean == "skip":
            if not ans_clean:
                ans_clean = "skip"
        else:
            if not ans_clean:
                _append_answer_audit(
                    session_key=sk,
                    current_index=int(s.get("current", 0) or 0),
                    action=action_clean,
                    answer_text=ans_clean,
                    is_skipped_answer=False,
                    status="rejected_empty_send",
                    reason="empty_payload",
                )
                return JSONResponse({"error": "Empty answer payload for send action."}, status_code=400)
        is_skipped_answer = action_clean == "skip" or ans_clean.strip().lower() in {"skip", "skipped", "[skipped]"}
        aa_meta = parse_auto_advance_meta(auto_advance_meta)
        if is_skipped_answer:
            convert, answer_text = skip_should_convert_to_answer(ans_clean, aa_meta)
            if convert and answer_text:
                logger.info("[SKIP] Converting skip into answered question")
                is_skipped_answer = False
                action_clean = "send"
                ans_clean = answer_text
        if is_skipped_answer and aa_meta:
            trigger = str(aa_meta.get("trigger") or "").strip().lower()
            if trigger == "no_response":
                allowed, block_reason = skip_allowed_by_speech_evidence(aa_meta)
                if not allowed:
                    _append_answer_audit(
                        session_key=sk,
                        current_index=int(s.get("current", 0) or 0),
                        action=action_clean,
                        answer_text=ans_clean,
                        is_skipped_answer=True,
                        status="rejected_speech_evidence",
                        reason=block_reason or "speech_detected",
                    )
                    return JSONResponse(
                        {
                            "error": "Skip blocked: candidate speech detected.",
                            "speech_blocked": True,
                            "reason": block_reason or "speech_detected",
                        },
                        status_code=409,
                    )
        if action_clean == "send" and ans_clean.strip().lower() in {"skip", "skipped", "[skipped]"} and len(ans_clean) <= 12:
            _append_answer_audit(
                session_key=sk,
                current_index=int(s.get("current", 0) or 0),
                action=action_clean,
                answer_text=ans_clean,
                is_skipped_answer=True,
                status="rejected_skip_token_on_send",
                reason="skip_token_requires_skip_action",
            )
            return JSONResponse(
                {"error": "Use Skip Question for skipped turns. Send Response requires a transcript or typed answer."},
                status_code=400,
            )
        try:
            logger.info(
                "interview.answer.received",
                extra={
                    "event": "interview.answer.received",
                    "session_key": sk,
                    "current_index": int(s.get("current", 0) or 0),
                    "action": action_clean,
                    "answer_len": len(ans_clean),
                    "is_skipped_answer": bool(is_skipped_answer),
                    "answer_preview": ans_clean[:120],
                },
            )
        except Exception:
            pass

        previous_question = s["questions"][s["current"]]
        session_meta_early = s.setdefault("meta", {})
        if aa_meta:
            if aa_meta.get("auto_submitted_on_timeout"):
                session_meta_early["auto_submitted_on_timeout"] = True
                session_meta_early["boundary_question_index"] = turn_index
            record_auto_advance_turn_event(
                s,
                question_index=turn_index,
                question_text=previous_question,
                answer_transcript="" if is_skipped_answer else ans_clean,
                event={
                    **aa_meta,
                    "skipped": bool(is_skipped_answer),
                    "auto_submitted": bool(aa_meta.get("auto_submitted")) and not is_skipped_answer,
                    "auto_submitted_on_timeout": bool(aa_meta.get("auto_submitted_on_timeout")),
                    "evaluation_started": bool(
                        not is_warmup_index(session_meta_early, turn_index) and not is_skipped_answer
                    ),
                },
            )
        if is_skipped_answer:
            _record_skipped_turn(
                s,
                question_index=turn_index,
                question_text=previous_question,
                reason=str(skip_reason or "").strip() or "Candidate skipped manually",
            )

        s["answers"].append(ans_clean)
        _append_answer_audit(
            session_key=sk,
            current_index=turn_index,
            action=action_clean,
            answer_text=ans_clean,
            is_skipped_answer=is_skipped_answer,
            status="saved",
            reason=str(skip_reason or "").strip() if is_skipped_answer else "",
        )
        logger.info(
            "[FLOW] ANSWER_SAVED" if not is_skipped_answer else "[FLOW] QUESTION_SKIPPED",
            extra={
                "event": "interview.answer.saved" if not is_skipped_answer else "interview.question.skipped",
                "session_key": sk,
                "question_index": turn_index + 1,
                "action": action_clean,
            },
        )
        remember_asked_question(s, previous_question)
        meta = s.get("meta", {})
        is_manual_session = str(meta.get("question_source") or "") == "manual"
        is_warmup_turn = is_warmup_index(meta, s["current"])
        if not is_warmup_turn and not is_skipped_answer:
            append_interview_turn(
                interview_id=str(meta.get("interview_id", "")),
                question=previous_question,
                answer=ans_clean,
                skills=meta.get("jd_skills", []),
            )
            _schedule_turn_evaluation(s, previous_question, ans_clean)
        s["current"] += 1
        idx_next = s["current"]
        if s.get("finalizing"):
            _persist_interview_progress(s, status="submitting")
            return {"status": "ok", "answered": len(s["answers"]), "finalizing": True}
        jd_skills = meta.get("jd_skills", [])
        seen_skills = set()
        for i in range(min(idx_next, len(s["questions"]))):
            det = detect_skill_from_question(s["questions"][i], jd_skills)
            if det:
                seen_skills.add(det)
        missing_skills = [skill for skill in jd_skills if skill and skill not in seen_skills]

        if not is_manual_session and missing_skills and idx_next < len(s["questions"]):
            next_skill = detect_skill_from_question(s["questions"][idx_next], jd_skills)
            if next_skill not in missing_skills:
                from utils.question_uniqueness import regenerate_unique_fallback_question

                replacement = regenerate_unique_fallback_question(s, avoid=list(s["questions"]))
                if replacement:
                    s["questions"][idx_next] = replacement
                    logger.info(
                        "[DYNAMIC] Regenerating Question",
                        extra={"index": idx_next + 1, "skill": missing_skills[0], "question": replacement[:180]},
                    )

        if (
            not is_manual_session
            and not is_warmup_turn
            and not is_skipped_answer
            and not s.get("finalizing")
            and bool(meta.get("adaptive_next_question", False))
            and idx_next < len(s["questions"])
        ):
            anchor_skill = detect_skill_from_question(previous_question, jd_skills)
            followup_skills = [anchor_skill] if anchor_skill else jd_skills[:1]
            prior_qs = list(s["questions"])
            qa_lines = []
            for i in range(max(0, idx_next - 3), idx_next):
                if i < len(s["questions"]) and i < len(s["answers"]):
                    qa_lines.append(f"Q: {s['questions'][i]}\nA: {s['answers'][i]}")
            recent_transcript = "\n\n".join(qa_lines)
            follow_q = ""
            if not meta.get("safe_mode", True):
                try:
                    follow_q = generate_followup_with_model(
                        jd=str(meta.get("jd_text", "")),
                        jd_skills=followup_skills,
                        previous_question=previous_question,
                        previous_answer=ans_clean,
                        model=str(meta.get("model", "gpt-4o-mini")),
                        recent_transcript=recent_transcript,
                        avoid_questions=prior_qs,
                        coach_hints="\n".join(
                            [x for x in [coach_hints_text(), str(meta.get("template_prompt") or "")] if str(x or "").strip()]
                        )[:5000],
                    )
                except Exception:
                    follow_q = ""
            if not follow_q or question_too_similar(follow_q, prior_qs):
                follow_q = generate_followup_fallback(
                    followup_skills,
                    ans_clean,
                    int(meta.get("followups_added", 0)) + 1,
                    previous_question,
                )
            if follow_q and not question_too_similar(follow_q, prior_qs):
                _replace_question_slot(s, idx_next, follow_q)
                meta["followups_added"] = int(meta.get("followups_added", 0)) + 1
                meta["pending_tts_invalidate"] = True

        if (
            _adaptive_followup_enabled()
            and not is_warmup_turn
            and not is_skipped_answer
            and not s.get("finalizing")
            and not is_manual_session
            and not missing_skills
            and meta.get("followup_mode", False)
            and meta.get("followups_added", 0) < meta.get("max_followups", 0)
        ):
            anchor_skill = detect_skill_from_question(previous_question, jd_skills)
            followup_skills = [anchor_skill] if anchor_skill else jd_skills[:1]
            jd_text = meta.get("jd_text", "")
            model = meta.get("model", "gpt-4o-mini")
            safe_mode = meta.get("safe_mode", True)
            follow_idx = int(meta.get("followups_added", 0))
            prior_qs = list(s["questions"])
            qa_lines = []
            for i in range(max(0, idx_next - 3), idx_next):
                if i < len(s["questions"]) and i < len(s["answers"]):
                    qa_lines.append(f"Q: {s['questions'][i]}\nA: {s['answers'][i]}")
            recent_transcript = "\n\n".join(qa_lines)

            follow_q = ""
            coach = coach_hints_text()
            if not safe_mode:
                try:
                    follow_q = generate_followup_with_model(
                        jd=jd_text,
                        jd_skills=followup_skills,
                        previous_question=previous_question,
                        previous_answer=ans_clean,
                        model=model,
                        recent_transcript=recent_transcript,
                        avoid_questions=prior_qs,
                        coach_hints=coach,
                    )
                except Exception:
                    follow_q = ""
            if not follow_q or question_too_similar(follow_q, prior_qs):
                follow_q = generate_followup_fallback(
                    followup_skills, ans_clean, follow_idx + 1, previous_question
                )
            if follow_q and not question_too_similar(follow_q, prior_qs):
                if s["current"] < len(s["questions"]):
                    _replace_question_slot(s, s["current"], follow_q)
                meta["followups_added"] = meta.get("followups_added", 0) + 1
                meta["pending_tts_invalidate"] = True

        if not s.get("finalizing") and not is_skipped_answer:
            _expand_time_mode_pool(s)
        _persist_interview_progress(s, status="in_progress")

        next_payload = None
        if not s.get("finalizing"):
            next_payload = next_question_payload(s)
            if meta.get("pending_tts_invalidate"):
                next_payload["tts_invalidate"] = True
                meta["pending_tts_invalidate"] = False
            logger.info(
                "[FLOW] NEXT_QUESTION_FETCHED",
                extra={
                    "event": "interview.next_question.fetched",
                    "session_key": sk,
                    "next_index": int(s.get("current", 0) or 0) + 1,
                    "has_question": bool((next_payload or {}).get("question")),
                },
            )

        try:
            logger.info(
                "interview.turn.completed",
                extra={
                    "event": "interview.turn.completed",
                    "session_key": sk,
                    "question_index": turn_index + 1,
                    "action": action_clean,
                    "skipped": bool(is_skipped_answer),
                    "answered": bool(not is_skipped_answer),
                    "transcript_len": len(ans_clean),
                    "evaluation_started": bool(not is_warmup_turn and not is_skipped_answer),
                },
            )
        except Exception:
            pass

        return {
            "status": "ok",
            "answered": len(s["answers"]),
            "skipped": is_skipped_answer,
            "next": next_payload,
        }


def _append_pending_answer_on_submit(session: dict, raw_answer: str) -> bool:
    """
    May 2026: fast client termination may POST the in-progress answer with /submit
    instead of waiting for /answer — append once if the session is still open.
    """
    ans = str(raw_answer or "").strip()
    if not ans or is_time_limit_system_message(ans):
        return False
    if session.get("completed"):
        return False
    qs = session.get("questions") or []
    cur = int(session.get("current", 0))
    if not qs or cur >= len(qs):
        return False
    answers = list(session.get("answers") or [])
    if len(answers) > cur:
        return False
    session["answers"].append(ans)
    remember_asked_question(session, qs[cur])
    return True


def _record_boundary_question_meta(
    session: dict,
    *,
    time_expired: bool,
    finalize_via: str,
    auto_saved: bool,
    pending_appended: bool,
) -> None:
    """Tag the active question when the interview ends (timer or manual)."""
    if not pending_appended:
        return
    answers = session.get("answers") or []
    if not answers:
        return
    idx = len(answers) - 1
    ans = answers[idx]
    if not answer_turn_was_attempted(ans):
        return
    meta = session.setdefault("meta", {})
    qs = session.get("questions") or []
    meta["boundary_question_index"] = idx
    meta["boundary_question_text"] = qs[idx] if idx < len(qs) else ""
    meta["boundary_answer_text"] = ans
    via = "timer" if time_expired else (str(finalize_via or "").strip().lower() or "manual")
    meta["boundary_saved_via"] = via
    meta["boundary_auto_saved"] = bool(auto_saved)
    if time_expired:
        meta["auto_submitted_on_timeout"] = True
        meta["boundary_label"] = "Auto-submitted on timeout"
    else:
        meta["boundary_label"] = "Final Question Saved Automatically"


def _compute_boundary_report_turn(session: dict) -> int | None:
    """1-based turn index in the HR report Q/A list for the boundary question."""
    meta = session.get("meta") or {}
    bidx = meta.get("boundary_question_index")
    if bidx is None:
        return None
    raw_q = list(session.get("questions") or [])
    raw_a = list(session.get("answers") or [])
    full_q, full_a = filter_out_warmups(raw_q, raw_a, meta)
    q_rep, a_rep, _ = align_qa_to_answered_turns(full_q, full_a)
    target_q = meta.get("boundary_question_text") or (raw_q[bidx] if bidx < len(raw_q) else "")
    target_a = meta.get("boundary_answer_text") or (raw_a[bidx] if bidx < len(raw_a) else "")
    for i in range(len(q_rep)):
        if q_rep[i] == target_q and (not target_a or (i < len(a_rep) and a_rep[i] == target_a)):
            return i + 1
    if q_rep:
        return len(q_rep)
    return None


def _attach_boundary_question_to_report(session: dict, result: dict) -> dict:
    meta = session.get("meta") or {}
    if meta.get("boundary_question_index") is None:
        return result
    out = dict(result) if isinstance(result, dict) else {}
    turn_no = _compute_boundary_report_turn(session)
    label = str(meta.get("boundary_label") or "Boundary Question Evaluated")
    evaluated = answer_turn_is_valid_for_scoring(meta.get("boundary_answer_text") or "")
    out["boundary_question"] = {
        "label": label,
        "report_turn": turn_no,
        "saved_via": meta.get("boundary_saved_via", ""),
        "auto_saved": bool(meta.get("boundary_auto_saved")),
        "evaluated": evaluated,
    }
    if turn_no is not None:
        pq = out.get("per_question")
        if not isinstance(pq, list):
            pq = out.get("question_evaluations")
        if isinstance(pq, list):
            ti = int(turn_no) - 1
            if 0 <= ti < len(pq) and isinstance(pq[ti], dict):
                row = dict(pq[ti])
                row["boundary_label"] = label
                row["boundary_auto_saved"] = bool(meta.get("boundary_auto_saved"))
                if meta.get("auto_submitted_on_timeout"):
                    row["auto_submitted_on_timeout"] = True
                pq = list(pq)
                pq[ti] = row
                out["per_question"] = pq
    return out


def _background_finalize_report(session_snapshot: dict) -> None:
    """Run evaluation + report persistence after the HTTP response returns."""
    try:
        out = _finalize_interview_snapshot(session_snapshot, reason="background_finalize", final_status="completed")
        logger.info(
            "interview.report.generated.background",
            extra={
                "event": "interview.report.generated.background",
                "interview_id": out.get("interview_id", ""),
                "candidate_name": (session_snapshot.get("meta", {}).get("candidate_profile", {}) or {}).get("name", "Candidate"),
            },
        )
    except Exception as exc:
        logging.getLogger(__name__).warning(
            "background report finalization failed: %s", exc, exc_info=True
        )


@app.post("/submit")
def submit(
    request: Request,
    background_tasks: BackgroundTasks,
    background_finalize: str = Form("false"),
    pending_answer: str = Form(""),
    time_expired: str = Form("false"),
    finalize_via: str = Form(""),
    boundary_auto_saved: str = Form("false"),
):
    payload, auth_err = _require_user(request, {"hr", "candidate"})
    if auth_err:
        return auth_err
    device_err = _enforce_invite_device_binding(request, payload)
    if device_err:
        return device_err
    sk = _session_key_from_payload(payload)
    s = sessions.get(sk)
    if not s:
        invite_token_from_token = str((payload or {}).get("invite_token") or "").strip()
        recovered = get_interview_progress_by_invite(AUTH_DB_TARGET, invite_token_from_token) if invite_token_from_token else None
        s = _session_from_progress(recovered)
        if not s:
            return {"error": "No active session."}
        sessions[sk] = s
    if s.get("finalizing") and s.get("report_result"):
        existing_id = str((s.get("meta", {}) or {}).get("interview_id") or "")
        return {"status": "submitted", "report_ready": True, "interview_id": existing_id, "reused_report": True}
    s["finalizing"] = True
    time_expired_flag = str(time_expired or "").strip().lower() in ("1", "true", "yes", "on")
    finalize_via_clean = str(finalize_via or "").strip().lower()
    auto_saved_flag = str(boundary_auto_saved or "").strip().lower() in ("1", "true", "yes", "on")
    pending_appended = _append_pending_answer_on_submit(s, pending_answer)
    _record_boundary_question_meta(
        s,
        time_expired=time_expired_flag,
        finalize_via=finalize_via_clean,
        auto_saved=auto_saved_flag,
        pending_appended=pending_appended,
    )
    meta = s.get("meta", {})
    bg_finalize = str(background_finalize or "").strip().lower() in ("1", "true", "yes", "on")
    final_status = "completed"
    reason = "candidate_submitted"
    if meta.get("termination_reason") or s.get("terminated"):
        final_status = "terminated"
        reason = str(meta.get("termination_reason") or "terminated")
    elif time_expired_flag:
        final_status = "partially_completed" if int(s.get("current") or 0) < len(s.get("questions") or []) else "completed"
        reason = "time_expired"
    elif int(s.get("current") or 0) < len(s.get("questions") or []):
        final_status = "partially_completed"
        reason = "candidate_ended_early"

    candidate_fast_finalize = bg_finalize or str((payload or {}).get("role") or "").lower() == "candidate"
    logger.info(
        "[FLOW] INTERVIEW_ENDED",
        extra={"event": "interview.ended", "interview_id": str(meta.get("interview_id") or "")},
    )
    logger.info(
        "[FLOW] REPORT_STARTED",
        extra={"event": "report.generation.started", "interview_id": str(meta.get("interview_id") or "")},
    )
    if candidate_fast_finalize:
        session_snapshot = copy.deepcopy(s)
        out = _persist_fast_final_report(s, reason=reason, final_status=final_status)
        background_tasks.add_task(_upgrade_interview_report_background, session_snapshot, reason, final_status)
    else:
        out = _finalize_interview_snapshot(s, reason=reason, final_status=final_status)
    if bg_finalize:
        out["background_finalize"] = False
        out["synchronous_finalize"] = True
    logger.info(
        "[FLOW] REPORT_COMPLETED",
        extra={
            "event": "report.generation.completed",
            "interview_id": str(meta.get("interview_id") or ""),
            "fast_finalize": bool(out.get("fast_finalize")),
            "report_ready": bool(out.get("report_ready")),
        },
    )
    logger.info(
        "interview.submitted",
        extra={
            "event": "interview.submitted",
            "interview_id": str(meta.get("interview_id", "")),
            "candidate_name": (meta.get("candidate_profile", {}) or {}).get("name", "Candidate"),
            "final_status": final_status,
        },
    )
    try:
        if not candidate_fast_finalize:
            sessions.pop(sk, None)
    except Exception:
        pass
    return out


def _latest_submitted_session() -> dict | None:
    """Any in-memory session with submitted=True (e.g. invite `inv:…` or HR `demo-session`)."""
    subs = [sess for sess in sessions.values() if sess.get("submitted")]
    if not subs:
        return None
    if len(subs) == 1:
        return subs[0]
    return max(
        subs,
        key=lambda sess: (
            str(sess.get("meta", {}).get("updated_at_ist", "") or sess.get("meta", {}).get("created_at_ist", "")),
            str(sess.get("meta", {}).get("interview_id", "")),
        ),
    )


def _report_response_from_persisted_record(rec: dict) -> dict:
    """HR unlock when server restarted (no in-memory session) but JSON records exist."""
    meta_out = {
        "interview_id": rec.get("id"),
        "candidate_profile": rec.get("candidate_profile", {}),
        "jd_skills": rec.get("skills", []),
        "jd_text": "",
        "model": rec.get("model", "gpt-4o-mini"),
    }
    evaluated_ist = {
        "ist_iso": rec.get("updated_at_ist") or "",
        "ist_date": rec.get("updated_date_ist") or "",
        "ist_time": rec.get("updated_time_ist") or "",
    }
    result = rec.get("report") or {}
    iid = str(rec.get("id", ""))
    qs = rec.get("questions") or []
    ans = rec.get("answers") or []
    return {
        "meta": meta_out,
        "interview_id": iid,
        "generated_at_ist": evaluated_ist["ist_iso"],
        "generated_date_ist": evaluated_ist["ist_date"],
        "generated_time_ist": evaluated_ist["ist_time"],
        "questions_count": len(qs),
        "answers_count": len(ans),
        "completion_status": _report_completion_status(rec, result if isinstance(result, dict) else {}),
        "final_status": str(rec.get("final_status") or ""),
        "report_status": str(rec.get("report_status") or ("ready" if result else "pending")),
        "result": result,
    }


@app.post("/report")
def report(request: Request, secret: str = Form(...)):
    _, auth_err = _require_user(request, {"hr"})
    if auth_err:
        return auth_err
    if not hmac.compare_digest(str(secret or ""), str(_effective_report_code() or "")):
        return {"error": "Unauthorized"}

    s = _latest_submitted_session()
    if not s:
        records = load_hr_records(DATA_FILE)
        candidates = [r for r in records if r.get("submitted") and r.get("report")]
        if candidates:
            latest = max(candidates, key=lambda r: str(r.get("updated_at", r.get("created_at", ""))))
            return _report_response_from_persisted_record(latest)
        return {"error": "No submitted interview found. Complete an interview first."}
    if not s.get("submitted"):
        return {"error": "Candidate has not submitted the interview yet."}

    meta = s.get("meta", {})
    if s.get("report_result"):
        result = s.get("report_result") or {}
        evaluated_ist = {
            "ist_iso": str(s.get("report_generated_at_ist", _now_ist_parts()["ist_iso"])),
            "ist_date": str(s.get("meta", {}).get("created_date_ist", "")),
            "ist_time": str(s.get("meta", {}).get("created_time_ist", "")),
        }
        record = build_report_record(s, result, _now_ist_parts())
        upsert_interview_record_snapshot(AUTH_DB_TARGET, record)
        _persist_hr_record_mirror(record)
        invalidate_hr_dashboard_cache()
    else:
        result, evaluated_ist, record = _evaluate_and_store_report(s)
    logger.info(
        "interview.report.generated",
        extra={
            "event": "interview.report.generated",
            "interview_id": record.get("id", ""),
            "candidate_name": record.get("candidate_name", "Candidate"),
        },
    )

    iid = str(meta.get("interview_id", ""))
    append_from_evaluation(meta.get("jd_skills", []), result, iid)

    return {
        "meta": s.get("meta", {}),
        "interview_id": iid,
        "generated_at_ist": evaluated_ist["ist_iso"],
        "generated_date_ist": evaluated_ist["ist_date"],
        "generated_time_ist": evaluated_ist["ist_time"],
        "questions_count": len(s["questions"]),
        "answers_count": len(s["answers"]),
        "result": result,
    }


@app.get("/hr-records")
def hr_records(request: Request):
    _, auth_err = _require_user(request, {"hr"})
    if auth_err:
        return auth_err
    _recover_interviews_once(limit=50)
    # Prefer DB-backed interview_records so HR dropdown matches Admin dashboard.
    snap = get_database_snapshot(AUTH_DB_TARGET, limit=1000)
    rows = (((snap or {}).get("tables", {}) or {}).get("interview_records", {}) or {}).get("rows", []) or []
    db_records: list[dict] = []
    for row in rows:
        payload = row.get("payload", None)
        rec: dict | None = None
        if isinstance(payload, dict):
            rec = payload
        elif isinstance(payload, str):
            rec = _safe_json_loads(payload)
        if not rec:
            continue
        if "id" not in rec:
            rec["id"] = row.get("id", "")
        db_records.append(rec)
    if db_records:
        return {"records": build_hr_records_summary(db_records), "source": "database"}

    records = load_hr_records(DATA_FILE)
    summary = build_hr_records_summary(records)
    return {"records": summary, "source": "records_file"}


@app.get("/hr/database")
def hr_database(request: Request, limit: int = 200):
    _, auth_err = _require_user(request, {"hr"})
    if auth_err:
        return auth_err
    return get_database_snapshot(AUTH_DB_TARGET, limit=limit)


@app.get("/hr/diagnostics/answer-integrity")
def hr_answer_integrity_diagnostics(
    request: Request,
    limit: int = 100,
    since_minutes: int = 240,
):
    _, auth_err = _require_user(request, {"hr"})
    if auth_err:
        return auth_err

    safe_limit = max(1, min(int(limit or 100), 500))
    safe_since = max(1, min(int(since_minutes or 240), 60 * 24 * 14))
    cutoff_ts = time.time() - (safe_since * 60)
    with _ANSWER_AUDIT_LOCK:
        entries = [e for e in list(_ANSWER_AUDIT) if float(e.get("ts") or 0) >= cutoff_ts]

    total = len(entries)
    send_saved = sum(1 for e in entries if e.get("status") == "saved" and e.get("action") == "send")
    skip_saved = sum(1 for e in entries if e.get("status") == "saved" and e.get("action") == "skip")
    empty_send_rejected = sum(1 for e in entries if e.get("status") == "rejected_empty_send")
    suspicious_send_saved_as_skip = sum(
        1 for e in entries
        if e.get("status") == "saved" and e.get("action") == "send" and e.get("is_skipped_answer") is True
    )

    recent = sorted(entries, key=lambda e: float(e.get("ts") or 0), reverse=True)[:safe_limit]
    return {
        "window_minutes": safe_since,
        "total_events": total,
        "summary": {
            "send_saved": send_saved,
            "skip_saved": skip_saved,
            "empty_send_rejected": empty_send_rejected,
            "suspicious_send_saved_as_skip": suspicious_send_saved_as_skip,
        },
        "healthy": suspicious_send_saved_as_skip == 0,
        "events": recent,
    }


def _safe_json_loads(text: str) -> dict | None:
    raw = (text or "").strip()
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _dash_status_from_report(report: dict | None, record: dict | None = None) -> str:
    rec = record or {}
    report_status = str(rec.get("report_status") or "").strip().lower()
    if report_status in {"generating", "pending", "ready_pending_ai"}:
        return "Generating"
    r = report or {}
    if r.get("report_upgrade_pending"):
        return "Generating"
    blob = f"{r.get('recommendation', '')} {r.get('overall_recommendation', '')} {r.get('fitment', '')}".lower()
    if not blob.strip():
        return "Pending Review"
    if "reject" in blob or "no hire" in blob or "not hire" in blob:
        return "Rejected"
    if "hire" in blob or "select" in blob or "strong" in blob:
        return "Selected"
    return "Pending Review"


def _merge_candidate_status_with_hr(ai_status: str, hr_decision: str | None) -> str:
    """When HR marks shortlist/reject/on_hold, surface that for candidate-level badges."""
    if hr_decision == "shortlist":
        return "Selected"
    if hr_decision == "reject":
        return "Rejected"
    # May 2026: "On Hold" is a deferred decision — it remains in the active
    # pipeline but is visually distinct from the model's default badge.
    if hr_decision == "on_hold":
        return "On Hold"
    return str(ai_status or "Pending Review")


def _effective_interview_status(record: dict, report: dict | None) -> str:
    """HR override on the interview payload wins over AI-derived dashboard status."""
    report_status = str((record or {}).get("report_status") or "").strip().lower()
    if report_status in {"generating", "pending", "ready_pending_ai"}:
        return "Generating"
    hr = str((record or {}).get("hr_interview_status") or "").strip()
    if hr in ("Selected", "Rejected", "Pending Review", "On Hold"):
        return hr
    return _dash_status_from_report(report, record)


def _report_completion_status(record: dict, report: dict | None) -> str:
    report_status = str((record or {}).get("report_status") or "").strip().lower()
    if report_status in {"generating", "pending", "ready_pending_ai"}:
        return "Generating"
    raw = str((record or {}).get("final_status") or (report or {}).get("report_type") or "").strip().lower()
    if raw == "terminated":
        return "Terminated"
    if raw == "abandoned":
        return "Abandoned"
    if raw == "recovered":
        return "Recovered"
    if raw in {"partially_completed", "partial", "early_ended"}:
        return "Partially Completed"
    if (record or {}).get("report") or report:
        return "Completed"
    return "Pending"


def _dash_score_from_report(report: dict | None) -> int:
    r = report or {}
    ss = r.get("scoring_summary") if isinstance(r.get("scoring_summary"), dict) else None
    if ss and ss.get("attempted_questions_only") and ss.get("mean_score_on_evaluated") is not None:
        try:
            m = float(ss["mean_score_on_evaluated"])
            if m >= 0:
                if m <= 10:
                    return int(round(m * 10))
                return int(round(m))
        except (TypeError, ValueError):
            pass
    raw = r.get("overall_score", None)
    if raw is None:
        raw = r.get("score", None)
    if raw is None:
        raw = r.get("final_score", None)
    try:
        n = float(raw)
    except (TypeError, ValueError):
        return 0
    if n <= 10:
        return int(round(n * 10))
    return int(round(n))


_HR_DASHBOARD_CACHE: dict[tuple[str, int], tuple[float, dict]] = {}
_HR_DASHBOARD_CACHE_LOCK = threading.Lock()
_HR_DASHBOARD_TTL_S = float(os.getenv("HR_DASHBOARD_TTL_S", "30"))


def invalidate_hr_dashboard_cache() -> None:
    """Drop the cached /hr/dashboard payload after a write."""
    with _HR_DASHBOARD_CACHE_LOCK:
        _HR_DASHBOARD_CACHE.clear()


@app.get("/hr/dashboard")
def hr_dashboard(request: Request, limit: int = 500):
    """
    Dashboard-ready API that returns:
    - candidates: [{id,name,email,role,interviews:[...]}]
    - sessions: [{id,name,date,category}]
    """
    user, auth_err = _require_user(request, {"hr"})
    if auth_err:
        return auth_err
    hr_user = str((user or {}).get("sub", "hr")).strip().lower() or "hr"
    _recover_interviews_once(limit=50)
    _cleanup_expired_integrity_rows(hr_user)

    bucket_limit = max(1, min(int(limit or 500), 1000))
    role_key = str((user or {}).get("role") or "hr").lower()
    cache_key = (role_key, bucket_limit)
    if _HR_DASHBOARD_TTL_S > 0:
        with _HR_DASHBOARD_CACHE_LOCK:
            cached = _HR_DASHBOARD_CACHE.get(cache_key)
            if cached and (time.monotonic() - cached[0]) < _HR_DASHBOARD_TTL_S:
                return cached[1]

    snap = get_database_snapshot(AUTH_DB_TARGET, limit=bucket_limit)
    rows = (((snap or {}).get("tables", {}) or {}).get("interview_records", {}) or {}).get("rows", []) or []

    candidates_by_key: dict[str, dict] = {}
    sessions_by_id: dict[str, dict] = {}
    template_title_cache: dict[str, str] = {}
    job_crm_cache: dict[str, dict[str, str]] = {}

    def _template_key(record: dict, default_label: str) -> tuple[str, str]:
        """Return (templateId, templateTitle) for an interview record."""
        jid = str(record.get("job_id") or "").strip()
        title = _resolved_job_title_for_record(record, template_title_cache) or ""
        title = title.strip()
        if not title:
            title = default_label
        tid = jid or f"title::{title.lower()}"
        return tid, title

    def _add_session_attendee(
        template_id: str,
        template_title: str,
        date: str,
        candidate_key: str,
        *,
        opportunity_id: str = "",
        customer_name: str = "",
    ) -> None:
        bucket = sessions_by_id.get(template_id)
        if bucket is None:
            sessions_by_id[template_id] = {
                "id": template_id,
                "name": template_title,
                "date": date,
                "category": "Template",
                "opportunityId": opportunity_id,
                "customerName": customer_name,
                "candidate_keys": {candidate_key} if candidate_key else set(),
            }
            return
        if candidate_key:
            bucket["candidate_keys"].add(candidate_key)
        if date and (not bucket.get("date") or date > str(bucket.get("date") or "")):
            bucket["date"] = date

    for row in rows:
        payload = row.get("payload", None)
        record: dict | None = None
        if isinstance(payload, dict):
            record = payload
        elif isinstance(payload, str):
            record = _safe_json_loads(payload)
        if not record:
            continue

        candidate_profile = record.get("candidate_profile", {}) or {}
        name = str(record.get("candidate_name") or candidate_profile.get("name") or "Candidate").strip() or "Candidate"
        email = str(record.get("candidate_email") or candidate_profile.get("email") or "Not available").strip() or "Not available"
        role = str(candidate_profile.get("role_hint") or record.get("candidate_role") or record.get("role") or "Candidate").strip() or "Candidate"

        key = (email.lower() if email != "Not available" else name.lower()) or "candidate"
        if key not in candidates_by_key:
            candidates_by_key[key] = {"id": key, "name": name, "email": email, "role": role, "interviews": []}

        rid = str(record.get("id") or row.get("id") or "").strip()
        difficulty = str(record.get("difficulty") or "Interview").strip()
        created_date = str(record.get("created_date_ist") or "").strip()
        created_at_ist = str(record.get("created_at_ist") or "").strip()
        date = created_date or (created_at_ist[:10] if created_at_ist else "")
        report = record.get("report") if isinstance(record.get("report"), dict) else {}
        skills = record.get("skills") if isinstance(record.get("skills"), list) else []

        template_id, template_title = _template_key(record, default_label=f"{role} • {difficulty}".strip())
        jid = str(record.get("job_id") or "").strip()
        crm = _job_crm_fields_for_job_id(jid, job_crm_cache) if jid else {"opportunityId": "", "customerName": ""}

        interview = {
            "id": rid,
            "sessionName": template_title,
            "templateId": template_id,
            "templateTitle": template_title,
            "opportunityId": crm.get("opportunityId") or "",
            "customerName": crm.get("customerName") or "",
            "date": date,
            "scheduled_at_local": str(record.get("scheduled_at_local") or "").strip(),
            "completed_at_ist": str(record.get("updated_at_ist") or record.get("updated_at") or "").strip(),
            "invite_token": str(record.get("invite_token") or "").strip(),
            "skills": skills,
            "score": _dash_score_from_report(report),
            "status": _effective_interview_status(record, report),
            "completion_status": _report_completion_status(record, report),
            "final_status": str(record.get("final_status") or "").strip(),
            "report_status": str(record.get("report_status") or ("ready" if report else "pending")).strip(),
        }
        candidates_by_key[key]["interviews"].append(interview)
        _add_session_attendee(
            template_id,
            template_title,
            date,
            key,
            opportunity_id=crm.get("opportunityId") or "",
            customer_name=crm.get("customerName") or "",
        )

    # Fallback: if DB snapshot has no usable interview_records yet, use file-based HR records.
    if not candidates_by_key:
        try:
            file_records = load_hr_records(DATA_FILE)
        except Exception:
            file_records = []
        for record in file_records or []:
            candidate_profile = record.get("candidate_profile", {}) or {}
            name = str(record.get("candidate_name") or candidate_profile.get("name") or "Candidate").strip() or "Candidate"
            email = str(record.get("candidate_email") or candidate_profile.get("email") or "Not available").strip() or "Not available"
            role = str(candidate_profile.get("role_hint") or record.get("candidate_role") or record.get("role") or "Candidate").strip() or "Candidate"
            key = (email.lower() if email != "Not available" else name.lower()) or "candidate"
            if key not in candidates_by_key:
                candidates_by_key[key] = {"id": key, "name": name, "email": email, "role": role, "interviews": []}

            rid = str(record.get("id") or "").strip()
            date = str(record.get("created_date_ist") or (record.get("created_at_ist") or "")[:10] or "").strip()
            report = record.get("report") if isinstance(record.get("report"), dict) else {}
            skills = record.get("skills") if isinstance(record.get("skills"), list) else []
            difficulty = str(record.get("difficulty") or "Interview").strip()
            template_id, template_title = _template_key(record, default_label=f"{role} • {difficulty}".strip())
            jid = str(record.get("job_id") or "").strip()
            crm = _job_crm_fields_for_job_id(jid, job_crm_cache) if jid else {"opportunityId": "", "customerName": ""}
            interview = {
                "id": rid,
                "sessionName": template_title,
                "templateId": template_id,
                "templateTitle": template_title,
                "opportunityId": crm.get("opportunityId") or "",
                "customerName": crm.get("customerName") or "",
                "date": date,
                "skills": skills,
                "score": _dash_score_from_report(report),
                "status": _effective_interview_status(record, report),
                "completion_status": _report_completion_status(record, report),
                "final_status": str(record.get("final_status") or "").strip(),
                "report_status": str(record.get("report_status") or ("ready" if report else "pending")).strip(),
            }
            candidates_by_key[key]["interviews"].append(interview)
            _add_session_attendee(
                template_id,
                template_title,
                date,
                key,
                opportunity_id=crm.get("opportunityId") or "",
                customer_name=crm.get("customerName") or "",
            )

    for c in candidates_by_key.values():
        c["interviews"].sort(key=lambda x: str(x.get("date", "")), reverse=True)
        # Header role should follow the latest interview's template, not a stale HR "candidate role" hint.
        invs = c.get("interviews") or []
        if invs:
            tt = str(invs[0].get("templateTitle") or "").strip()
            if tt:
                c["role"] = tt

    try:
        hr_map = list_hr_candidate_decisions(AUTH_DB_TARGET)
    except Exception:
        hr_map = {}
    for c in candidates_by_key.values():
        ck = str(c.get("id") or "").strip().lower()
        dec = hr_map.get(ck)
        c["hr_decision"] = dec if dec in ("shortlist", "reject", "on_hold") else None

    candidates = sorted(list(candidates_by_key.values()), key=lambda x: str(x.get("name", "")).lower())
    sessions_list: list[dict] = []
    for entry in sessions_by_id.values():
        candidate_keys = entry.pop("candidate_keys", set()) or set()
        entry["candidate_count"] = len(candidate_keys)
        sessions_list.append(entry)
    sessions = sorted(sessions_list, key=lambda x: (str(x.get("date") or ""), str(x.get("name") or "").lower()), reverse=True)
    source = "database" if rows else "records_file"
    payload = {"candidates": candidates, "sessions": sessions, "source": source}
    if _HR_DASHBOARD_TTL_S > 0:
        with _HR_DASHBOARD_CACHE_LOCK:
            _HR_DASHBOARD_CACHE[cache_key] = (time.monotonic(), payload)
    return payload


@app.get("/hr/candidates/suggest")
def hr_candidate_suggest(request: Request, q: str = "", limit: int = 10):
    _, auth_err = _require_interview_report_reader(request)
    if auth_err:
        return auth_err
    return {"candidates": search_candidate_suggestions(AUTH_DB_TARGET, q, limit)}


@app.get("/hr/interviews/{interview_id}")
def hr_interview_detail(request: Request, interview_id: str):
    """
    Return the complete stored interview session record for HR drill-down:
    questions, answers, per-turn info, and final report.
    """
    _, auth_err = _require_user(request, {"hr"})
    if auth_err:
        return auth_err
    rec = get_interview_record_payload(AUTH_DB_TARGET, interview_id)
    if not rec:
        return JSONResponse({"error": "Interview record not found."}, status_code=404)
    # Backward compatibility: ensure id present
    if "id" not in rec:
        rec["id"] = str(interview_id)
    crm = _enrich_record_with_job_crm(rec)
    rec["opportunityId"] = crm.get("opportunityId") or ""
    rec["customerName"] = crm.get("customerName") or ""
    return {"record": rec}


@app.patch("/hr/interviews/{interview_id}/status")
def hr_patch_interview_status(request: Request, interview_id: str, payload: dict = Body(default_factory=dict)):
    """
    Update HR-facing interview status (Selected / Pending Review / Rejected).
    Persists as hr_interview_status on the interview record payload.
    """
    _, auth_err = _require_user(request, {"hr"})
    if auth_err:
        return auth_err
    raw = payload.get("status")
    if raw is None or str(raw).strip() == "":
        return JSONResponse({"error": "status is required."}, status_code=400)
    try:
        updated = update_interview_hr_status(AUTH_DB_TARGET, interview_id, str(raw))
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    if not updated:
        return JSONResponse({"error": "Interview record not found."}, status_code=404)
    try:
        _sync_hr_decision_if_newest_interview(AUTH_DB_TARGET, interview_id, updated)
    except Exception as exc:
        logging.getLogger(__name__).warning("sync hr_decision from interview failed: %s", exc)
    # Carry the decision into the CRM as well. Without this the CRM Candidate
    # Profile keeps showing the AI's score-threshold verdict, so a candidate the
    # recruiter just marked Selected still reads "Failed".
    _push_decision_to_crm(
        decision=updated.get("hr_interview_status"),
        actor=request,
        interview_record_id=interview_id,
        invite_token=updated.get("invite_token"),
        candidate_email=updated.get("candidate_email") or updated.get("email"),
    )
    invalidate_hr_dashboard_cache()
    st = str(updated.get("hr_interview_status") or "")
    return {"status": "ok", "interview_id": str(interview_id), "interview_status": st}


def _delete_interview_runtime_artifacts(interview_id: str, record: dict | None) -> dict:
    rid = str(interview_id or "").strip()
    removed = {
        "hr_records_file": 0,
        "interview_schedule": 0,
        "proctor_reports": 0,
        "in_memory_sessions": 0,
        "learning_rows": 0,
    }
    if not rid:
        return removed

    removed["hr_records_file"] = int(delete_record_by_id(DATA_FILE, rid))

    invite_token = str((record or {}).get("invite_token") or "").strip()
    if invite_token:
        try:
            removed["interview_schedule"] = int(delete_interview_schedule_by_token(AUTH_DB_TARGET, invite_token))
        except Exception as err:
            logger.warning(
                "interview.delete.schedule_failed",
                extra={"event": "interview.delete.schedule_failed", "interview_id": rid, "error": str(err)},
            )

    try:
        for skey in list(sessions.keys()):
            sess = sessions.get(skey) or {}
            meta = sess.get("meta", {}) or {}
            if str(meta.get("interview_id") or "").strip() == rid:
                sessions.pop(skey, None)
                removed["in_memory_sessions"] += 1
    except Exception:
        pass

    try:
        reports = _load_proctor_reports()
        before = len(reports)
        keep = {
            k: v
            for k, v in (reports or {}).items()
            if str((v or {}).get("interviewId") or "").strip() != rid
        }
        if len(keep) != before:
            _save_proctor_reports(keep)
            removed["proctor_reports"] = before - len(keep)
    except Exception as err:
        logger.warning(
            "interview.delete.proctor_failed",
            extra={"event": "interview.delete.proctor_failed", "interview_id": rid, "error": str(err)},
        )

    try:
        if LEARNING_FILE.exists():
            kept_lines: list[str] = []
            for line in LEARNING_FILE.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    kept_lines.append(line)
                    continue
                if str((obj or {}).get("interview_id") or "").strip() == rid:
                    removed["learning_rows"] += 1
                    continue
                kept_lines.append(line)
            if removed["learning_rows"]:
                LEARNING_FILE.write_text("\n".join(kept_lines) + ("\n" if kept_lines else ""), encoding="utf-8")
    except Exception as err:
        logger.warning(
            "interview.delete.learning_failed",
            extra={"event": "interview.delete.learning_failed", "interview_id": rid, "error": str(err)},
        )

    return removed


@app.delete("/hr/interviews/{interview_id}")
def hr_interview_delete(request: Request, interview_id: str):
    """Delete one interview/report record and directly associated artifacts."""
    _, auth_err = _require_user(request, {"hr"})
    if auth_err:
        return auth_err
    rid = str(interview_id or "").strip()
    if not rid:
        return JSONResponse({"error": "Interview id is required."}, status_code=400)
    record = get_interview_record_payload(AUTH_DB_TARGET, rid)
    if not record:
        record = find_hr_record(load_hr_records(DATA_FILE), rid)
    if not record:
        return JSONResponse({"error": "Interview record not found."}, status_code=404)
    ok = delete_interview_record(AUTH_DB_TARGET, interview_id)
    removed = _delete_interview_runtime_artifacts(rid, record)
    invalidate_hr_dashboard_cache()
    return {
        "status": "ok",
        "deleted": True,
        "interview_id": rid,
        "removed": {
            "interview_records": 1 if ok else 0,
            **removed,
        },
    }


def _normalize_interview_score(report: dict | None) -> int:
    return _dash_score_from_report(report)


def _interview_duration_seconds(record: dict) -> int:
    """Best-effort duration: prefer explicit duration, otherwise updated - created."""
    raw = record.get("duration_sec") or (record.get("report") or {}).get("duration_sec")
    try:
        if raw is not None:
            n = int(float(raw))
            if n > 0:
                return n
    except (TypeError, ValueError):
        pass
    started = str(record.get("created_at") or record.get("created_at_ist") or "").strip()
    ended = str(record.get("updated_at") or record.get("updated_at_ist") or "").strip()
    if not started or not ended:
        return 0
    try:
        s = datetime.fromisoformat(started.replace("Z", "+00:00"))
        e = datetime.fromisoformat(ended.replace("Z", "+00:00"))
    except ValueError:
        return 0
    delta = (e - s).total_seconds()
    return int(delta) if delta > 0 else 0


def _hr_recommendation_from_report(report: dict | None) -> str:
    r = report or {}
    for key in ("recommendation", "overall_recommendation", "fitment", "hire_recommendation"):
        val = str(r.get(key) or "").strip()
        if val:
            return val
    return ""


def _interview_skill_breakdown(record: dict) -> list[dict]:
    report = record.get("report") if isinstance(record.get("report"), dict) else {}
    raw_breakdown = report.get("skill_scores") or report.get("skills_breakdown") or report.get("skills") or []
    out: list[dict] = []
    if isinstance(raw_breakdown, list):
        for item in raw_breakdown:
            if isinstance(item, dict):
                name = str(item.get("skill") or item.get("name") or "").strip()
                score = item.get("score")
                if not name:
                    continue
                try:
                    n = float(score)
                    n = round(n * 10) if n <= 10 else round(n)
                except (TypeError, ValueError):
                    n = 0
                out.append({"skill": name, "score": int(max(0, min(100, n)))})
    elif isinstance(raw_breakdown, dict):
        for name, score in raw_breakdown.items():
            try:
                n = float(score)
                n = round(n * 10) if n <= 10 else round(n)
            except (TypeError, ValueError):
                n = 0
            out.append({"skill": str(name), "score": int(max(0, min(100, n)))})
    return out


def _resolved_job_title_for_record(record: dict | None, title_cache: dict[str, str] | None = None) -> str:
    """Interview template display name: stored job_title, else resolve from job_id via job_templates."""
    if not record:
        return ""
    direct = str(record.get("job_title") or "").strip()
    if direct:
        return direct
    jid = str(record.get("job_id") or "").strip()
    if not jid:
        return ""
    if title_cache is not None and jid in title_cache:
        return title_cache[jid]
    cfg = get_job_template(AUTH_DB_TARGET, jid) or {}
    out = str(cfg.get("jobTitle") or "").strip()
    if title_cache is not None:
        title_cache[jid] = out
    return out


def _job_crm_fields_for_job_id(
    job_id: str,
    crm_cache: dict[str, dict[str, str]] | None = None,
) -> dict[str, str]:
    """Resolve opportunity/customer from job_templates by job_id."""
    jid = str(job_id or "").strip()
    if not jid:
        return {"opportunityId": "", "customerName": ""}
    if crm_cache is not None and jid in crm_cache:
        return crm_cache[jid]
    cfg = get_job_template(AUTH_DB_TARGET, jid) or {}
    out = {
        "opportunityId": str(cfg.get("opportunityId") or cfg.get("opportunity_id") or "").strip(),
        "customerName": str(cfg.get("customerName") or cfg.get("customer_name") or "").strip(),
    }
    if crm_cache is not None:
        crm_cache[jid] = out
    return out


def _enrich_record_with_job_crm(record: dict | None, crm_cache: dict[str, dict[str, str]] | None = None) -> dict[str, str]:
    if not isinstance(record, dict):
        return {"opportunityId": "", "customerName": ""}
    jid = str(record.get("job_id") or "").strip()
    return _job_crm_fields_for_job_id(jid, crm_cache)


def _interview_summary_payload(record: dict) -> dict:
    """Compact payload used for the timeline/Evaluations tab on the admin page."""
    report = record.get("report") if isinstance(record.get("report"), dict) else {}
    comm = report.get("communication_evaluation") if isinstance(report.get("communication_evaluation"), dict) else {}
    # Templates can declare that a role is not assessed on communication. Older
    # reports predate the flag, so absence means "was assessed" — the historic
    # behaviour — rather than defaulting the other way and blanking their scores.
    _assessed_communication = bool(report.get("communication_required", True))
    questions = record.get("questions") if isinstance(record.get("questions"), list) else []
    answers = record.get("answers") if isinstance(record.get("answers"), list) else []
    rid = str(record.get("id") or "")
    score = _normalize_interview_score(report)
    duration = _interview_duration_seconds(record)
    job_title = _resolved_job_title_for_record(record, None)
    crm = _enrich_record_with_job_crm(record)
    return {
        "id": rid,
        "job_title": job_title,
        "opportunityId": crm.get("opportunityId") or "",
        "customerName": crm.get("customerName") or "",
        "scheduled_at_local": str(record.get("scheduled_at_local") or "").strip(),
        "created_at": str(record.get("created_at") or "").strip(),
        "created_at_ist": str(record.get("created_at_ist") or "").strip(),
        "created_date_ist": str(record.get("created_date_ist") or "").strip(),
        "created_time_ist": str(record.get("created_time_ist") or "").strip(),
        "updated_at": str(record.get("updated_at") or "").strip(),
        "updated_at_ist": str(record.get("updated_at_ist") or "").strip(),
        "updated_date_ist": str(record.get("updated_date_ist") or "").strip(),
        "updated_time_ist": str(record.get("updated_time_ist") or "").strip(),
        "duration_sec": int(duration),
        "score": int(score),
        "status": _effective_interview_status(record, report),
        "difficulty": str(record.get("difficulty") or "").strip(),
        "model": str(record.get("model") or "").strip(),
        "skills": list(record.get("skills") or []),
        "questions_count": len(questions),
        "answers_count": len(answers),
        "submitted": bool(record.get("submitted")),
        "has_report": bool(report),
        "recommendation": _hr_recommendation_from_report(report),
        "summary": str(report.get("overall_summary") or report.get("summary") or report.get("feedback") or "").strip(),
        # Communication and confidence are both derived from the communication
        # evaluation, so both are None when the template turned it off. None
        # rather than 0 — a role that was never assessed on communication must
        # not read as having scored zero for it.
        "communication_required": _assessed_communication,
        "communication_score": (
            int(_normalize_interview_score({"overall_score": comm.get("communication_score") or comm.get("overall_score")}))
            if _assessed_communication else None
        ),
        "technical_score": int(_normalize_interview_score({"overall_score": report.get("technical_score") or report.get("overall_score")})),
        "confidence_score": (
            int(_normalize_interview_score({"overall_score": comm.get("presentation_score") or comm.get("confidence_score")}))
            if _assessed_communication else None
        ),
        "strengths": [str(x) for x in (report.get("strengths") or [])][:8],
        "weaknesses": [str(x) for x in (report.get("gaps") or report.get("weaknesses") or report.get("improvements") or [])][:8],
        "skill_breakdown": _interview_skill_breakdown(record),
        "excluded_questions_count": int(
            (report.get("scoring_summary") or {}).get("excluded_questions") or 0
        )
        if isinstance(report.get("scoring_summary"), dict)
        else 0,
    }


def _candidate_basic_profile(records: list[dict]) -> dict:
    """Fold the freshest non-empty profile fields across the candidate's records."""
    name = ""
    email = ""
    role = ""
    skills: list[str] = []
    seen_skills: set[str] = set()
    sorted_records = sorted(
        records,
        key=lambda r: str(r.get("updated_at_ist") or r.get("created_at_ist") or r.get("updated_at") or ""),
        reverse=True,
    )
    title_cache: dict[str, str] = {}
    template_titles: list[str] = []
    seen_titles_lower: set[str] = set()
    for rec in sorted_records:
        profile = rec.get("candidate_profile") or {}
        if not name:
            name = str(rec.get("candidate_name") or profile.get("name") or "").strip()
        if not email:
            cand_email = str(rec.get("candidate_email") or profile.get("email") or "").strip()
            if cand_email and cand_email.lower() != "not available":
                email = cand_email
        if not role:
            role = str(profile.get("role_hint") or rec.get("candidate_role") or rec.get("role") or "").strip()
        jt = _resolved_job_title_for_record(rec, title_cache)
        if jt:
            low = jt.lower()
            if low not in seen_titles_lower:
                seen_titles_lower.add(low)
                template_titles.append(jt)
        for sk in rec.get("skills") or []:
            s = str(sk or "").strip()
            if not s:
                continue
            low = s.lower()
            if low in seen_skills:
                continue
            seen_skills.add(low)
            skills.append(s)
    title_line = ""
    if template_titles:
        max_show = 8
        shown = template_titles[:max_show]
        title_line = " · ".join(shown)
        if len(template_titles) > max_show:
            title_line = f"{title_line} (+{len(template_titles) - max_show} more)"
    # Prefer the most recent interview's template name (sorted_records is newest-first).
    newest_resolved = _resolved_job_title_for_record(sorted_records[0], title_cache) if sorted_records else ""
    display_role = (
        newest_resolved.strip()
        or title_line
        or (role or "Candidate")
    )
    return {
        "name": name or "Candidate",
        "email": email or "Not available",
        "role": display_role,
        "skills": skills,
    }


def _candidate_history_for_id(candidate_id: str) -> list[dict]:
    """DB-first, file-fallback: returns every record matching the candidate id."""
    rows = list_interview_records_for_candidate(AUTH_DB_TARGET, candidate_id) or []
    if rows:
        return rows
    return list_records_for_candidate(DATA_FILE, candidate_id) or []


def _candidate_dashboard_key_from_record(rec: dict) -> str:
    """Same candidate key as /hr/dashboard (lower email, else lower name)."""
    if not rec:
        return "candidate"
    candidate_profile = rec.get("candidate_profile") or {}
    email = str(rec.get("candidate_email") or candidate_profile.get("email") or "Not available").strip() or "Not available"
    name = str(rec.get("candidate_name") or candidate_profile.get("name") or "Candidate").strip() or "Candidate"
    return (email.lower() if email != "Not available" else name.lower()) or "candidate"


def _primary_interview_id_for_candidate(cid: str) -> str | None:
    """Latest interview row id using the same ordering as /hr/candidates/.../interviews."""
    records = _candidate_history_for_id(cid)
    if not records:
        return None
    records = list(records)
    records.sort(
        key=lambda r: str(r.get("updated_at_ist") or r.get("created_at_ist") or r.get("updated_at") or ""),
        reverse=True,
    )
    rid = str((records[0] or {}).get("id") or "").strip()
    return rid or None


def _sync_hr_decision_if_newest_interview(db_target, interview_id: str, payload: dict) -> None:
    """When HR edits the primary (latest) interview row, mirror onto candidate-level hr_decision."""
    ck = _candidate_dashboard_key_from_record(payload)
    primary = _primary_interview_id_for_candidate(ck)
    iid = str(interview_id or "").strip()
    if not primary or primary != iid:
        return
    st = str(payload.get("hr_interview_status") or "").strip()
    if st == "Selected":
        set_hr_candidate_decision(db_target, ck, "shortlist")
    elif st == "Rejected":
        set_hr_candidate_decision(db_target, ck, "reject")
    elif st == "On Hold":
        # May 2026 — deferred outcome at the interview level mirrors as
        # `on_hold` at the candidate level.
        set_hr_candidate_decision(db_target, ck, "on_hold")
    else:
        set_hr_candidate_decision(db_target, ck, None)


def _sync_latest_interview_from_hr_decision(db_target, candidate_id: str, decision: str | None) -> None:
    """After PUT hr-decision, keep the latest interview's hr_interview_status aligned."""
    rid = _primary_interview_id_for_candidate(candidate_id)
    if not rid:
        return
    if decision == "shortlist":
        update_interview_hr_status(db_target, rid, "selected")
    elif decision == "reject":
        update_interview_hr_status(db_target, rid, "rejected")
    elif decision == "on_hold":
        update_interview_hr_status(db_target, rid, "on_hold")
    else:
        update_interview_hr_status(db_target, rid, "clear")


@app.get("/hr/candidates/{candidate_id}/interviews")
def hr_candidate_interviews(request: Request, candidate_id: str, limit: int = 50, offset: int = 0,
                            iid: str | None = None):
    """
    Complete interview history for a candidate.

    - candidate_id: lower-cased email (or name fallback) used by the dashboard
    - iid: optional interview-record id hint — when the candidate id finds
      nothing (stale link, resume email differing from the scheduled email),
      the record itself names the right candidate key and the lookup self-heals
    - returns the candidate profile plus interviews newest-first, lazy-loadable via offset/limit
    """
    _, auth_err = _require_interview_report_reader(request)
    if auth_err:
        return auth_err

    cid = (candidate_id or "").strip().lower()
    if not cid:
        return JSONResponse({"error": "Candidate id is required."}, status_code=400)

    records = _candidate_history_for_id(cid)
    if not records and (iid or "").strip():
        # SELF-HEAL (27 Aug 2026): CRM report links used to carry the resume's
        # email while interview_records is keyed by the SCHEDULED email — the
        # record id is unambiguous, so derive the true key from the record.
        try:
            payload = get_interview_record_payload(AUTH_DB_TARGET, str(iid).strip())
            if payload:
                healed = _candidate_dashboard_key_from_record(payload)
                if healed and healed != cid:
                    records = _candidate_history_for_id(healed)
                    if records:
                        cid = healed
        except Exception:
            pass  # fall through to the 404 below
    if not records:
        return JSONResponse({"error": "Candidate not found."}, status_code=404)

    safe_limit = max(1, min(int(limit or 50), 200))
    safe_offset = max(0, int(offset or 0))

    records.sort(
        key=lambda r: str(r.get("updated_at_ist") or r.get("created_at_ist") or r.get("updated_at") or ""),
        reverse=True,
    )
    total = len(records)
    page = records[safe_offset : safe_offset + safe_limit]

    profile = _candidate_basic_profile(records)
    timeline = [_interview_summary_payload(r) for r in page]
    score_avg = (
        round(sum(item["score"] for item in timeline) / len(timeline)) if timeline else 0
    )
    latest_status = timeline[0]["status"] if timeline else "Pending Review"
    hr_decision = get_hr_candidate_decision(AUTH_DB_TARGET, cid)
    header_status = _merge_candidate_status_with_hr(latest_status, hr_decision)

    return {
        "candidate": {
            "id": cid,
            "name": profile["name"],
            "email": profile["email"],
            "role": profile["role"],
            "skills": profile["skills"],
            "status": header_status,
            "hr_decision": hr_decision,
            "total_interviews": total,
            "avg_score": score_avg,
        },
        "interviews": timeline,
        "pagination": {
            "limit": safe_limit,
            "offset": safe_offset,
            "total": total,
            "has_more": (safe_offset + safe_limit) < total,
        },
    }


@app.get("/hr/candidates/{candidate_id}/interviews/{interview_id}")
def hr_candidate_interview_detail(request: Request, candidate_id: str, interview_id: str):
    """Full interview detail (Q/A + report) scoped to one candidate."""
    _, auth_err = _require_interview_report_reader(request)
    if auth_err:
        return auth_err
    rec = get_interview_record_payload(AUTH_DB_TARGET, interview_id)
    if not rec:
        rec = find_hr_record(load_hr_records(DATA_FILE), interview_id)
    if not rec:
        return JSONResponse({"error": "Interview record not found."}, status_code=404)
    cid = (candidate_id or "").strip().lower()
    profile = rec.get("candidate_profile") or {}
    rec_email = str(rec.get("candidate_email") or profile.get("email") or "").strip().lower()
    rec_name = str(rec.get("candidate_name") or profile.get("name") or "").strip().lower()
    if cid and cid not in {rec_email, rec_name}:
        return JSONResponse({"error": "Interview does not belong to this candidate."}, status_code=403)
    if "id" not in rec:
        rec["id"] = str(interview_id)
    return {"record": rec}


def _ensure_interview_strengths_weaknesses_record(rec: dict, *, force: bool = False) -> dict:
    """Load or build persisted strengths/weaknesses analysis without changing scores."""
    report = rec.get("report") if isinstance(rec.get("report"), dict) else {}
    questions = list(rec.get("questions") or [])
    answers = list(rec.get("answers") or [])
    model = str(rec.get("model") or os.getenv("INTERVIEW_OPENAI_MODEL") or "gpt-4o-mini").strip()
    updated = attach_strengths_weaknesses_analysis(
        report,
        questions,
        answers,
        model=model,
        force_regenerate=force,
    )
    if updated is not report:
        rec = dict(rec)
        rec["report"] = updated
        upsert_interview_record_snapshot(AUTH_DB_TARGET, rec)
        try:
            _persist_hr_record_mirror(rec)
        except Exception:
            pass
        invalidate_hr_dashboard_cache()
    return rec


@app.get("/hr/candidates/{candidate_id}/interviews/{interview_id}/strengths-weaknesses")
def hr_candidate_strengths_weaknesses(request: Request, candidate_id: str, interview_id: str):
    """Return persisted per-question and overall strengths/weaknesses (generate once if missing)."""
    _, auth_err = _require_interview_report_reader(request)
    if auth_err:
        return auth_err
    rec = get_interview_record_payload(AUTH_DB_TARGET, interview_id)
    if not rec:
        rec = find_hr_record(load_hr_records(DATA_FILE), interview_id)
    if not rec:
        return JSONResponse({"error": "Interview record not found."}, status_code=404)
    cid = (candidate_id or "").strip().lower()
    profile = rec.get("candidate_profile") or {}
    rec_email = str(rec.get("candidate_email") or profile.get("email") or "").strip().lower()
    rec_name = str(rec.get("candidate_name") or profile.get("name") or "").strip().lower()
    if cid and cid not in {rec_email, rec_name}:
        return JSONResponse({"error": "Interview does not belong to this candidate."}, status_code=403)
    report_before = rec.get("report") if isinstance(rec.get("report"), dict) else {}
    cached = bool(
        isinstance(report_before.get("strengths_weaknesses_analysis"), dict)
        and report_before["strengths_weaknesses_analysis"].get("complete")
    )
    rec = _ensure_interview_strengths_weaknesses_record(rec)
    analysis = (rec.get("report") or {}).get("strengths_weaknesses_analysis") or {}
    return {"analysis": analysis, "cached": cached}


@app.patch("/hr/candidates/{candidate_id}/interviews/{interview_id}/per-question/{question_index}/score-exclusion")
async def hr_exclude_question_from_score(
    request: Request,
    candidate_id: str,
    interview_id: str,
    question_index: int,
):
    """Exclude one evaluated question from final score aggregates (HR moderation)."""
    payload, auth_err = _require_user(request, {"hr"})
    if auth_err:
        return auth_err
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}

    rec = get_interview_record_payload(AUTH_DB_TARGET, interview_id)
    if not rec:
        rec = find_hr_record(load_hr_records(DATA_FILE), interview_id)
    if not rec:
        return JSONResponse({"error": "Interview record not found."}, status_code=404)

    cid = (candidate_id or "").strip().lower()
    profile = rec.get("candidate_profile") or {}
    rec_email = str(rec.get("candidate_email") or profile.get("email") or "").strip().lower()
    rec_name = str(rec.get("candidate_name") or profile.get("name") or "").strip().lower()
    if cid and cid not in {rec_email, rec_name}:
        return JSONResponse({"error": "Interview does not belong to this candidate."}, status_code=403)

    report = rec.get("report") if isinstance(rec.get("report"), dict) else {}
    if not report:
        return JSONResponse({"error": "Interview report not ready yet."}, status_code=409)

    try:
        qidx = int(question_index)
    except (TypeError, ValueError):
        return JSONResponse({"error": "Invalid question index."}, status_code=400)
    if qidx < 1:
        return JSONResponse({"error": "question_index must be >= 1."}, status_code=400)

    excluded_raw = body.get("excluded", True)
    excluded = excluded_raw is not False and str(excluded_raw).strip().lower() not in ("0", "false", "no", "off")

    reason = str(body.get("reason") or body.get("excluded_reason") or "").strip()
    manager = (
        str(payload.get("name") or payload.get("display_name") or payload.get("sub") or "").strip()
        or "HR Manager"
    )

    from utils.score_exclusion import exclude_question_from_score, include_question_in_score

    try:
        if excluded:
            updated_report = exclude_question_from_score(
                report,
                list(rec.get("questions") or []),
                list(rec.get("answers") or []),
                question_index=qidx,
                excluded_by=manager,
                reason=reason,
            )
        else:
            updated_report = include_question_in_score(
                report,
                list(rec.get("questions") or []),
                list(rec.get("answers") or []),
                question_index=qidx,
                included_by=manager,
            )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    rec = dict(rec)
    rec["report"] = updated_report
    model = str(rec.get("model") or os.getenv("INTERVIEW_OPENAI_MODEL") or "gpt-4o-mini").strip()
    rec["report"] = attach_strengths_weaknesses_analysis(
        updated_report,
        list(rec.get("questions") or []),
        list(rec.get("answers") or []),
        model=model,
        force_regenerate=True,
    )

    upsert_interview_record_snapshot(AUTH_DB_TARGET, rec)
    try:
        _persist_hr_record_mirror(rec)
    except Exception:
        pass
    invalidate_hr_dashboard_cache()

    logger.info(
        "[SCORE] Question score inclusion updated",
        extra={
            "event": "hr.score_exclusion",
            "interview_id": interview_id,
            "question_index": qidx,
            "excluded": excluded,
            "manager": manager,
            "reason": reason[:200],
        },
    )

    return {
        "status": "ok",
        "interview_id": interview_id,
        "question_index": qidx,
        "excluded": excluded,
        "record": rec,
        "scoring_summary": (rec.get("report") or {}).get("scoring_summary"),
        "overall_score": (rec.get("report") or {}).get("overall_score"),
        "recommendation": (rec.get("report") or {}).get("recommendation"),
    }


@app.put("/hr/candidates/{candidate_id}/hr-decision")
def hr_put_candidate_hr_decision(request: Request, candidate_id: str, payload: dict = Body(default_factory=dict)):
    """Persist HR shortlist/reject for a candidate (dashboard key = lower email or name)."""
    _, auth_err = _require_interview_report_reader(request)
    if auth_err:
        return auth_err
    cid = (candidate_id or "").strip().lower()
    if not cid:
        return JSONResponse({"error": "Candidate id is required."}, status_code=400)
    raw = payload.get("decision")
    if raw is None:
        normalized: str | None = None
    else:
        s = str(raw).strip().lower().replace(" ", "_").replace("-", "_")
        if s in ("", "null", "none", "clear"):
            normalized = None
        elif s in ("shortlist", "reject"):
            normalized = s
        elif s in ("on_hold", "hold", "onhold"):
            # May 2026: deferred decision — kept in pipeline but flagged.
            normalized = "on_hold"
        else:
            return JSONResponse({"error": "decision must be shortlist, reject, on_hold, or null."}, status_code=400)

    records = _candidate_history_for_id(cid)
    if not records:
        return JSONResponse({"error": "Candidate not found."}, status_code=404)
    try:
        set_hr_candidate_decision(AUTH_DB_TARGET, cid, normalized)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    try:
        _sync_latest_interview_from_hr_decision(AUTH_DB_TARGET, cid, normalized)
    except Exception as exc:
        logging.getLogger(__name__).warning("sync interview hr status from hr_decision failed: %s", exc)
    # And into the CRM, so Candidate Profiles > AI Interview reflects it at once.
    # `cid` is the lower-cased email for dashboard-keyed candidates, which is
    # what the CRM link lookup falls back to.
    newest = records[0] if records else {}
    _push_decision_to_crm(
        decision=normalized,
        actor=request,
        interview_record_id=newest.get("id"),
        invite_token=newest.get("invite_token"),
        candidate_email=cid,
    )
    invalidate_hr_dashboard_cache()
    return {"status": "ok", "candidate_id": cid, "hr_decision": normalized}


@app.delete("/hr/candidates/{candidate_id}")
def hr_candidate_delete(request: Request, candidate_id: str):
    """
    Permanently delete a candidate and every related row.

    Sweeps DB tables (interview_records, interview_schedule, login_data,
    registration_data) inside a transaction, then wipes file-backed JSON,
    proctor reports, learning rows, and the in-memory invite session.
    """
    _, auth_err = _require_user(request, {"hr"})
    if auth_err:
        return auth_err
    _enforce_crm_roles(request, "TA", "HR")  # Reports (destructive): Admin/TA/HR

    cid = (candidate_id or "").strip().lower()
    if not cid:
        return JSONResponse({"error": "Candidate id is required."}, status_code=400)

    pre_records = _candidate_history_for_id(cid)
    if not pre_records:
        return JSONResponse({"error": "Candidate not found."}, status_code=404)

    interview_ids = {str(r.get("id") or "").strip() for r in pre_records if r.get("id")}
    interview_ids.discard("")

    try:
        db_summary = cascade_delete_candidate(AUTH_DB_TARGET, cid)
    except Exception as err:
        logger.error(
            "candidate.delete.db_failed",
            extra={"event": "candidate.delete.db_failed", "candidate_id": cid, "error": str(err)},
        )
        return JSONResponse(
            {"error": f"Database delete failed; nothing was removed: {err}"},
            status_code=500,
        )

    file_removed = delete_records_for_candidate(DATA_FILE, cid)

    proctor_removed = 0
    try:
        reports = _load_proctor_reports()
        before = len(reports)
        keep = {
            k: v
            for k, v in (reports or {}).items()
            if str(k or "").strip().lower() != cid
            and str((v or {}).get("candidateId") or "").strip().lower() != cid
        }
        if len(keep) != before:
            _save_proctor_reports(keep)
            proctor_removed = before - len(keep)
    except Exception as err:
        logger.warning(
            "candidate.delete.proctor_failed",
            extra={"event": "candidate.delete.proctor_failed", "candidate_id": cid, "error": str(err)},
        )

    sessions_removed = 0
    try:
        for skey in list(sessions.keys()):
            sess = sessions.get(skey) or {}
            meta = sess.get("meta", {}) or {}
            sess_email = str((meta.get("candidate_profile") or {}).get("email") or "").strip().lower()
            sess_name = str((meta.get("candidate_profile") or {}).get("name") or "").strip().lower()
            iid = str(meta.get("interview_id") or "").strip()
            if sess_email == cid or sess_name == cid or (iid and iid in interview_ids):
                sessions.pop(skey, None)
                sessions_removed += 1
    except Exception:
        pass

    learning_removed = 0
    try:
        if interview_ids and LEARNING_FILE.exists():
            kept_lines: list[str] = []
            for line in LEARNING_FILE.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    kept_lines.append(line)
                    continue
                iid = str((obj or {}).get("interview_id") or "").strip()
                if iid and iid in interview_ids:
                    learning_removed += 1
                    continue
                kept_lines.append(line)
            if learning_removed:
                LEARNING_FILE.write_text("\n".join(kept_lines) + ("\n" if kept_lines else ""), encoding="utf-8")
    except Exception as err:
        logger.warning(
            "candidate.delete.learning_failed",
            extra={"event": "candidate.delete.learning_failed", "candidate_id": cid, "error": str(err)},
        )

    logger.info(
        "candidate.deleted",
        extra={
            "event": "candidate.deleted",
            "candidate_id": cid,
            "interview_records": db_summary.get("interview_records", 0),
            "file_records": file_removed,
            "schedules": db_summary.get("interview_schedule", 0),
        },
    )

    return {
        "status": "ok",
        "deleted": True,
        "candidate_id": cid,
        "removed": {
            **db_summary,
            "hr_records_file": int(file_removed),
            "proctor_reports": int(proctor_removed),
            "in_memory_sessions": int(sessions_removed),
            "learning_rows": int(learning_removed),
            "interview_ids": sorted(interview_ids),
        },
    }


@app.get("/network-info")
def network_info(request: Request):
    """Return LAN-friendly URLs for quick sharing."""
    if _is_production_env():
        _, auth_err = _require_user(request, {"hr", "admin", "manager"})
        if auth_err:
            return auth_err
    ips: list[str] = []
    try:
        host = socket.gethostname()
        for info in socket.getaddrinfo(host, None):
            ip = info[4][0]
            try:
                addr = ipaddress.ip_address(ip)
            except ValueError:
                continue
            if addr.version != 4:
                continue
            if addr.is_loopback or addr.is_link_local or addr.is_multicast or addr.is_unspecified:
                continue
            if ip not in ips:
                ips.append(ip)
    except Exception:
        ips = []
    base_urls = [f"http://{ip}:8010" for ip in ips]
    return {
        "port": 8010,
        "local": "http://127.0.0.1:8010",
        "lan": base_urls,
        "admin": [f"{u}/admin" for u in base_urls],
    }


@app.get("/masters/opportunities")
def master_opportunities(request: Request, q: str = "", limit: int = 20):
    _, auth_err = _require_user(request, {"hr"})
    if auth_err:
        return auth_err
    return {"items": search_master_values(AUTH_DB_TARGET, "opportunity", q, limit)}


@app.post("/masters/opportunities")
def master_opportunity_create(request: Request, payload: dict = Body(default_factory=dict)):
    user, auth_err = _require_user(request, {"hr"})
    if auth_err:
        return auth_err
    value = str(payload.get("value") or payload.get("opportunityId") or payload.get("opportunity_id") or "").strip()
    item = upsert_master_value(AUTH_DB_TARGET, "opportunity", value, str((user or {}).get("sub") or ""))
    if not item:
        return JSONResponse({"error": "Opportunity ID is required."}, status_code=400)
    return {"status": "ok", "item": item}


@app.get("/masters/customers")
def master_customers(request: Request, q: str = "", limit: int = 20):
    _, auth_err = _require_user(request, {"hr"})
    if auth_err:
        return auth_err
    return {"items": search_master_values(AUTH_DB_TARGET, "customer", q, limit)}


@app.post("/masters/customers")
def master_customer_create(request: Request, payload: dict = Body(default_factory=dict)):
    user, auth_err = _require_user(request, {"hr"})
    if auth_err:
        return auth_err
    value = str(payload.get("value") or payload.get("customerName") or payload.get("customer_name") or "").strip()
    item = upsert_master_value(AUTH_DB_TARGET, "customer", value, str((user or {}).get("sub") or ""))
    if not item:
        return JSONResponse({"error": "Customer Name is required."}, status_code=400)
    return {"status": "ok", "item": item}


@app.post("/job/config")
async def job_config(
    request: Request,
    jobId: str = Form(""),
    jobTitle: str = Form(""),
    requiredSkills: str = Form(""),
    optionalSkills: str = Form(""),
    opportunityId: str = Form(""),
    customerName: str = Form(""),
    expMin: int = Form(0),
    expMax: int = Form(0),
    difficulty: str = Form("medium"),
    numQ: int = Form(5),
    followupMode: str = Form("false"),
    interviewMode: str = Form("technical"),
    domain: str = Form(""),
    jdText: str = Form(""),
    templateInstructions: str = Form(""),
    weights: str = Form(""),
    timingMode: str = Form("count"),
    timeLimitSec: int = Form(0),
    micAlwaysOn: str = Form("false"),
    showSpokenText: str = Form("false"),
    enableTranscriptInput: str = Form(""),
    questionType: str = Form("dynamic"),
    manualQuestions: str = Form("[]"),
    generatedPrompt: str = Form(""),
    editedPrompt: str = Form(""),
    promptVersion: int = Form(1),
    promptHistory: str = Form("[]"),
):
    user_payload, auth_err = _require_user(request, {"hr", "manager", "admin"})
    if auth_err:
        return auth_err
    _enforce_crm_roles(request, "RMG")  # Template management: Admin/RMG
    weights_obj = {}
    try:
        weights_obj = json.loads(weights) if (weights or "").strip() else {}
    except json.JSONDecodeError:
        weights_obj = {}
    mq_list: list = []
    mq_raw = (manualQuestions or "").strip()
    if mq_raw:
        try:
            parsed_mq = json.loads(mq_raw)
            if isinstance(parsed_mq, list):
                mq_list = parsed_mq
        except json.JSONDecodeError:
            mq_list = []
    transcript_toggle_raw = enableTranscriptInput if str(enableTranscriptInput).strip() else showSpokenText
    role_api = normalize_interview_mode(interviewMode)
    prompt_context = build_template_prompt_context(
        role=jobTitle,
        experience=f"{int(expMin or 0)}-{int(expMax or 0)} years",
        required_skills=[s.strip() for s in str(requiredSkills or "").split(",") if s.strip()],
        optional_skills=[s.strip() for s in str(optionalSkills or "").split(",") if s.strip()],
        difficulty=difficulty,
        interview_type=role_api,
        customer_name=customerName,
        opportunity_id=opportunityId,
        template_instructions=_resolve_template_instructions_param(templateInstructions, jdText),
        technology_stack=str((weights_obj or {}).get("intelligenceTechStack") or ""),
        interview_mode=role_api,
    )
    fresh_generated = build_default_template_prompt(prompt_context)
    stale_client_generated = sanitize_prompt_input(generatedPrompt)
    edited_prompt_clean = sanitize_prompt_input(editedPrompt)
    using_custom_prompt = bool(edited_prompt_clean) and edited_prompt_clean not in {
        stale_client_generated,
        fresh_generated,
    }
    generated_prompt_clean = fresh_generated
    if not using_custom_prompt:
        edited_prompt_clean = ""
    if len(generated_prompt_clean) > 12000 or len(edited_prompt_clean) > 12000:
        return JSONResponse({"error": "Prompt is too large. Limit is 12000 characters."}, status_code=400)
    prompt_history_list: list = []
    try:
        parsed_hist = json.loads(promptHistory or "[]")
        if isinstance(parsed_hist, list):
            prompt_history_list = parsed_hist
    except (json.JSONDecodeError, TypeError):
        prompt_history_list = []
    prompt_updated_by = str((user_payload or {}).get("sub") or "").strip()
    prompt_updated_at = datetime.now(timezone.utc).isoformat()
    prompt_version_safe = max(1, min(int(promptVersion or 1), 5000))
    if edited_prompt_clean:
        prompt_history_list = (
            list(prompt_history_list)[-40:]
            + [
                {
                    "version": prompt_version_safe,
                    "updated_by": prompt_updated_by,
                    "updated_at": prompt_updated_at,
                    "edited_prompt": edited_prompt_clean[:12000],
                }
            ]
        )[-50:]
    job = upsert_job_template(
        AUTH_DB_TARGET,
        {
            "jobId": jobId,
            "jobTitle": jobTitle,
            "requiredSkills": requiredSkills,
            "optionalSkills": optionalSkills,
            "opportunityId": opportunityId,
            "customerName": customerName,
            "expMin": expMin,
            "expMax": expMax,
            "difficulty": difficulty,
            "numQ": numQ,
            "followupMode": str(followupMode).strip().lower() in {"1", "true", "yes", "on"},
            "interviewMode": interviewMode,
            "domain": domain,
            "jdText": jdText,
            "templateInstructions": _resolve_template_instructions_param(templateInstructions, jdText),
            "weights": weights_obj,
            "timingMode": timingMode,
            "timeLimitSec": int(timeLimitSec or 0),
            "micAlwaysOn": str(micAlwaysOn).strip().lower() in {"1", "true", "yes", "on"},
            "enableTranscriptInput": str(transcript_toggle_raw).strip().lower() in {"1", "true", "yes", "on"},
            "showSpokenText": str(transcript_toggle_raw).strip().lower() in {"1", "true", "yes", "on"},
            "questionType": questionType,
            "manualQuestions": mq_list,
            "createdBy": str((user_payload or {}).get("sub") or ""),
            "generatedPrompt": generated_prompt_clean,
            "editedPrompt": edited_prompt_clean,
            "promptVersion": prompt_version_safe,
            "promptUpdatedBy": prompt_updated_by,
            "promptUpdatedAt": prompt_updated_at,
            "promptHistory": prompt_history_list,
        },
    )
    auto_fulfilled = _auto_fulfil_template_request(
        job_id=str(job.get("jobId") or jobId or ""),
        job_title=jobTitle,
        opportunity_opp_id=opportunityId,
        username=str((user_payload or {}).get("sub") or ""),
    )
    return {"status": "ok", "job": job, "auto_fulfilled_request": auto_fulfilled}


def _auto_fulfil_template_request(job_id: str, job_title: str,
                                  opportunity_opp_id: str, username: str) -> str | None:
    """RMG saved a template carrying an Opportunity ID → fulfil the matching
    Pending_RMG template request automatically, so 'Create template' is ONE
    step instead of create-then-go-back-and-Fulfil. Best-effort: any failure
    logs and returns None — saving the template must never break on the CRM.

    Returns the fulfilled request's tr_number, or None.
    """
    opp_id = (opportunity_opp_id or "").strip()
    if not opp_id or not (job_id or "").strip():
        return None
    try:
        from sqlalchemy import func as _sa_func, select as _select, text as _sa_text
        from crm_db import get_session_factory
        from models import Opportunity as _Opp, TemplateRequest as _TR
        from models.template_requests import TemplateRequestStatus as _TRS

        session = get_session_factory()()
        try:
            row = session.execute(
                _select(_TR, _Opp)
                .join(_Opp, _Opp.id == _TR.opportunity_id)
                .where(
                    _TR.status == _TRS.PENDING_RMG,
                    _sa_func.lower(_Opp.opp_id) == opp_id.lower(),
                )
                .order_by(_TR.id.desc())
            ).first()
            if row is None:
                return None
            tr = row[0]
            uid = session.execute(
                _sa_text("SELECT id FROM registration_data WHERE LOWER(username) = LOWER(:u)"),
                {"u": username},
            ).scalar()
            tr.template_job_id = job_id.strip()
            tr.template_name = (job_title or "").strip() or job_id.strip()
            tr.status = _TRS.TEMPLATE_READY
            tr.fulfilled_by = uid
            tr.fulfilled_at = datetime.now(timezone.utc)
            try:
                from services.notify import notify_user as _notify_user
                if tr.requested_by and tr.requested_by != uid:
                    _notify_user(
                        session, tr.requested_by,
                        f"Template ready for {tr.tr_number}",
                        f"RMG created and linked template '{tr.template_name}'. Trigger AI L1 when ready.",
                        f"/template-requests/{tr.id}",
                    )
            except Exception:
                pass
            session.commit()
            return tr.tr_number
        finally:
            session.close()
    except Exception as exc:  # noqa: BLE001 — CRM mirror is best-effort
        logging.getLogger(__name__).warning("Template-request auto-fulfil skipped: %s", exc)
        return None


@app.post("/job/template/sample-questions")
@_rl.limit("20/minute")
async def template_sample_questions(
    request: Request,
    requiredSkills: str = Form(""),
    optionalSkills: str = Form(""),
    difficulty: str = Form("medium"),
    numQ: int = Form(5),
    jdText: str = Form(""),
    templateInstructions: str = Form(""),
    expMin: int = Form(0),
    expMax: int = Form(0),
    questionCategories: str = Form("[]"),
    targetRole: str = Form(""),
    seniorityLevel: str = Form(""),
    technicalStack: str = Form(""),
    avoidHistory: str = Form("[]"),
    interviewMode: str = Form("technical"),
    generatedPrompt: str = Form(""),
    editedPrompt: str = Form(""),
):
    _, auth_err = _require_user(request, {"hr"})
    if auth_err:
        return auth_err
    _enforce_crm_roles(request, "RMG")  # Template authoring: Admin/RMG
    required_list = [s.strip().lower() for s in str(requiredSkills or "").split(",") if s.strip()]
    optional_list = [s.strip().lower() for s in str(optionalSkills or "").split(",") if s.strip()]
    if not required_list:
        return JSONResponse({"error": "Required skills are empty."}, status_code=400)
    seen: set[str] = set()
    skills: list[str] = []
    for sk in required_list + optional_list:
        if sk not in seen:
            seen.add(sk)
            skills.append(sk)
    skills = skills[:15]

    cat_ids: list[str] = []
    try:
        raw_cats = json.loads(questionCategories or "[]")
        if isinstance(raw_cats, list):
            cat_ids = [str(x).strip() for x in raw_cats if str(x).strip()]
    except (json.JSONDecodeError, TypeError):
        cat_ids = []

    role_line = str(targetRole or "").strip()
    seniority = str(seniorityLevel or "").strip()
    stack = str(technicalStack or "").strip()
    faux_weights = {
        "questionCategories": cat_ids,
        "intelligenceTargetRole": role_line,
        "intelligenceSeniority": seniority,
        "intelligenceTechStack": stack,
    }

    if cat_ids:
        base = clamp_count_mode_questions(numQ or 5)
        n = min(MAX_COUNT_MODE_QUESTIONS, max(base, len(cat_ids)))
    else:
        n = max(1, min(int(numQ or 5), 5))
    diff = str(difficulty or "medium").strip().lower() or "medium"
    if diff not in {"easy", "medium", "hard"}:
        diff = "medium"
    jd_text = str(jdText or "").strip() or "Generate interview questions aligned to required skills."
    augmented_jd = _jd_with_intelligence_suite(jd_text, faux_weights, role_fallback=role_line)
    template_experience = f"{expMin}-{expMax} years" if expMax > 0 else ""
    if seniority:
        template_experience = f"{seniority}; {template_experience}".strip("; ")
    prompt_ctx = build_template_prompt_context(
        role=role_line,
        experience=template_experience,
        required_skills=skills,
        optional_skills=[],
        difficulty=diff,
        interview_type=normalize_interview_mode(interviewMode),
        customer_name="",
        opportunity_id="",
        template_instructions=_resolve_template_instructions_param(templateInstructions, jd_text),
        technology_stack=stack,
        interview_mode=normalize_interview_mode(interviewMode),
    )
    fresh_generated = build_default_template_prompt(prompt_ctx)
    stale_client_generated = sanitize_prompt_input(generatedPrompt)
    edited_prompt = sanitize_prompt_input(editedPrompt)
    using_custom_prompt = bool(edited_prompt) and edited_prompt not in {
        stale_client_generated,
        fresh_generated,
    }
    template_prompt = render_prompt_preview(
        edited_prompt if using_custom_prompt else fresh_generated,
        prompt_ctx,
    )
    template_custom, validation_skills = _template_generation_options(
        edited=edited_prompt,
        # `generated_prompt` did not exist — this raised NameError whenever the
        # branch ran. The parallel call site below passes `fresh_generated`,
        # which is the freshly built default prompt this compares against.
        generated=fresh_generated,
        effective=template_prompt,
        form_skills=skills,
    )

    has_ai = openai_key_configured("question")
    coach_hints_text()
    model = str(os.getenv("INTERVIEW_OPENAI_MODEL") or "gpt-4o-mini").strip() or "gpt-4o-mini"
    safe_mode_on = str(os.getenv("INTERVIEW_SAFE_MODE", "false")).lower() in {"1", "true", "yes", "on"}
    resolved_domains = _resolve_domain_titles([str(cid).strip() for cid in cat_ids if str(cid).strip()])

    avoid_list: list[str] = []
    try:
        raw_avoid = json.loads(avoidHistory or "[]")
        if isinstance(raw_avoid, list):
            avoid_list = [str(x).strip() for x in raw_avoid if str(x).strip()]
    except (json.JSONDecodeError, TypeError):
        avoid_list = []

    variety_seed = f"{int(time.time() * 1000)}-{secrets.token_hex(3)}"

    cache_key_payload = {
        "skills": skills,
        "diff": diff,
        "n": n,
        "role": role_line,
        "seniority": seniority,
        "stack": stack,
        "exp": template_experience,
        "jd_head": (jd_text or "")[:512],
        "domains": list(resolved_domains),
        "model": model,
        "prompt_sig": hashlib.sha256(template_prompt.encode("utf-8")).hexdigest()[:20],
    }
    cache_key = response_cache.make_key("sample_questions", cache_key_payload)
    no_cache = bool(avoid_list) or str(request.query_params.get("nocache") or "").lower() in {"1", "true", "yes"}
    cached_questions: list[str] | None = None
    if not no_cache:
        cached_val = response_cache.get(AUTH_DB_TARGET, cache_key)
        if isinstance(cached_val, list) and cached_val:
            cached_questions = [str(q) for q in cached_val if str(q).strip()][:n]

    if cached_questions and len(cached_questions) >= max(1, n // 2):
        questions = cached_questions
    else:
        if has_ai and not safe_mode_on:
            sample_mode = normalize_interview_mode(interviewMode)
            questions = await run_in_threadpool(
                _generate_interview_questions,
                interview_mode=sample_mode,
                jd_text=augmented_jd,
                cv_text="",
                difficulty=diff,
                n=n,
                model=model,
                skills=skills,
                coach_hints="",
                experience=template_experience,
                domain_categories=resolved_domains or None,
                raw_passthrough=True,
                temperature=0.9,
                variety_seed=variety_seed,
                role=role_line,
                tech_stack=stack,
                template_prompt=template_prompt,
                avoid_history=avoid_list or None,
                template_custom=template_custom,
                validation_skills=validation_skills,
            )
        elif template_custom:
            questions = []
        else:
            questions = await run_in_threadpool(
                generate_questions_fallback, augmented_jd, "", diff, n, required_skills=skills
            )
        questions = [q for q in questions if q][:n]
        if questions and not no_cache:
            response_cache.set(AUTH_DB_TARGET, cache_key, "sample_questions", questions)

    domain_names = list(resolved_domains)
    domain_assignments: list[str] = []
    if domain_names:
        for i in range(len(questions)):
            domain_assignments.append(domain_names[i % len(domain_names)])
    else:
        domain_assignments = ["" for _ in questions]

    return {
        "questions": questions,
        "domains": domain_names,
        "domainAssignments": domain_assignments,
        "skillsUsed": skills,
        "requiredSkills": required_list,
        "optionalSkills": optional_list,
        "effectivePrompt": template_prompt,
        "charCount": len(template_prompt),
        "tokenEstimate": estimate_tokens(template_prompt),
    }


@app.post("/job/template/prompt-preview")
async def template_prompt_preview(
    request: Request,
    jobTitle: str = Form(""),
    requiredSkills: str = Form(""),
    optionalSkills: str = Form(""),
    expMin: int = Form(0),
    expMax: int = Form(0),
    difficulty: str = Form("medium"),
    interviewMode: str = Form("technical"),
    jdText: str = Form(""),
    templateInstructions: str = Form(""),
    customerName: str = Form(""),
    opportunityId: str = Form(""),
    technologyStack: str = Form(""),
    generatedPrompt: str = Form(""),
    editedPrompt: str = Form(""),
):
    _, auth_err = _require_user(request, {"hr", "manager", "admin"})
    if auth_err:
        return auth_err
    _enforce_crm_roles(request, "RMG")  # Template authoring: Admin/RMG
    mode = normalize_interview_mode(interviewMode)
    ctx = build_template_prompt_context(
        role=jobTitle,
        experience=f"{int(expMin or 0)}-{int(expMax or 0)} years",
        required_skills=[s.strip() for s in str(requiredSkills or "").split(",") if s.strip()],
        optional_skills=[s.strip() for s in str(optionalSkills or "").split(",") if s.strip()],
        difficulty=difficulty,
        interview_type=mode,
        customer_name=customerName,
        opportunity_id=opportunityId,
        template_instructions=_resolve_template_instructions_param(templateInstructions, jdText),
        technology_stack=technologyStack,
        interview_mode=mode,
    )
    # Always rebuild the default prompt from current form context so Summary
    # fields (e.g. Template Instructions) update the preview in real time.
    fresh_generated = build_default_template_prompt(ctx)
    stale_client_generated = sanitize_prompt_input(generatedPrompt)
    edited = sanitize_prompt_input(editedPrompt)
    using_custom_prompt = bool(edited) and edited not in {
        stale_client_generated,
        fresh_generated,
    }
    if using_custom_prompt:
        generated = fresh_generated
        effective = edited
    else:
        generated = fresh_generated
        edited = ""
        effective = fresh_generated
    preview = render_prompt_preview(effective, ctx)
    return {
        "generatedPrompt": generated,
        "editedPrompt": edited,
        "effectivePrompt": effective,
        "previewPrompt": preview,
        "usingCustomPrompt": using_custom_prompt,
        "charCount": len(effective),
        "tokenEstimate": estimate_tokens(effective),
    }


@app.post("/job/template/test-prompt")
@_rl.limit("20/minute")
async def template_test_prompt(
    request: Request,
    requiredSkills: str = Form(""),
    optionalSkills: str = Form(""),
    difficulty: str = Form("medium"),
    numQ: int = Form(15),
    jdText: str = Form(""),
    templateInstructions: str = Form(""),
    expMin: int = Form(0),
    expMax: int = Form(0),
    interviewMode: str = Form("technical"),
    targetRole: str = Form(""),
    technicalStack: str = Form(""),
    generatedPrompt: str = Form(""),
    editedPrompt: str = Form(""),
):
    _, auth_err = _require_user(request, {"hr", "manager", "admin"})
    if auth_err:
        return auth_err
    _enforce_crm_roles(request, "RMG")  # Template authoring: Admin/RMG
    required_list = [s.strip().lower() for s in str(requiredSkills or "").split(",") if s.strip()]
    optional_list = [s.strip().lower() for s in str(optionalSkills or "").split(",") if s.strip()]
    skills = []
    seen: set[str] = set()
    for s in required_list + optional_list:
        if s and s not in seen:
            seen.add(s)
            skills.append(s)
    if not skills:
        return JSONResponse({"error": "Required skills are empty."}, status_code=400)
    mode = normalize_interview_mode(interviewMode)
    ctx = build_template_prompt_context(
        role=targetRole,
        experience=f"{int(expMin or 0)}-{int(expMax or 0)} years",
        required_skills=skills,
        optional_skills=[],
        difficulty=difficulty,
        interview_type=mode,
        customer_name="",
        opportunity_id="",
        template_instructions=_resolve_template_instructions_param(templateInstructions, jdText),
        technology_stack=technicalStack,
        interview_mode=mode,
    )
    fresh_generated = build_default_template_prompt(ctx)
    stale_client_generated = sanitize_prompt_input(generatedPrompt)
    edited = sanitize_prompt_input(editedPrompt)
    using_custom_prompt = bool(edited) and edited not in {
        stale_client_generated,
        fresh_generated,
    }
    effective = render_prompt_preview(
        edited if using_custom_prompt else fresh_generated,
        ctx,
    )
    template_custom, validation_skills = _template_generation_options(
        edited=edited if using_custom_prompt else "",
        generated=fresh_generated,
        effective=effective,
        form_skills=skills,
    )
    model = str(os.getenv("INTERVIEW_OPENAI_MODEL") or "gpt-4o-mini").strip() or "gpt-4o-mini"
    safe_mode_on = str(os.getenv("INTERVIEW_SAFE_MODE", "false")).lower() in {"1", "true", "yes", "on"}
    has_ai = openai_key_configured("question")
    # Review-step preview: generate 15–20 questions so HR can sanity-check the prompt.
    test_n = max(15, min(20, int(numQ or 15)))
    if has_ai and not safe_mode_on:
        q = await run_in_threadpool(
            _generate_interview_questions,
            interview_mode=mode,
            jd_text=jdText or "Generate interview questions aligned to required skills.",
            cv_text="",
            difficulty=str(difficulty or "medium"),
            n=test_n,
            model=model,
            skills=skills,
            coach_hints="",
            experience=f"{int(expMin or 0)}-{int(expMax or 0)} years",
            role=str(targetRole or ""),
            tech_stack=str(technicalStack or ""),
            avoid_history=[],
            template_prompt=effective,
            variety_seed=f"{int(time.time() * 1000)}-{secrets.token_hex(3)}",
            template_custom=template_custom,
            validation_skills=validation_skills,
        )
    elif template_custom:
        q = []
    else:
        q = await run_in_threadpool(
            generate_questions_fallback,
            jdText or "Generate interview questions aligned to required skills.",
            "",
            str(difficulty or "medium"),
            test_n,
            skills,
        )
    questions = [str(item).strip() for item in (q or []) if str(item).strip()][:test_n]
    first = questions[0] if questions else ""
    if template_custom and not questions and has_ai and not safe_mode_on:
        return JSONResponse(
            {
                "error": "Could not generate sample questions from your custom prompt. "
                "Try a clearer instruction or use Reset to Default.",
                "effectivePrompt": effective,
            },
            status_code=422,
        )
    return {
        "status": "ok",
        "sampleQuestion": first,
        "sampleQuestions": questions,
        "questionCount": len(questions),
        "requestedCount": test_n,
        "effectivePrompt": effective,
        "charCount": len(effective),
        "tokenEstimate": estimate_tokens(effective),
    }


@app.get("/job/configs")
def job_configs(request: Request):
    _, auth_err = _require_user(request, {"hr", "manager", "admin"})
    if auth_err:
        return auth_err
    return {"jobs": list_job_templates(AUTH_DB_TARGET)}


@app.get("/job/config/{jobId}")
def job_config_get(request: Request, jobId: str):
    # Load a single template for the edit form. The absence of this route was the
    # root cause of "template details disappear after saving": the frontend edit
    # form fetched /job/config/{id}, got a 404/HTML miss, fell back to a list
    # endpoint that never returns {"job": ...}, so the form was never hydrated and
    # a subsequent Save overwrote the record with empty values.
    _, auth_err = _require_user(request, {"hr", "manager", "admin"})
    if auth_err:
        return auth_err
    _enforce_crm_roles(request, "TA", "HR", "RMG")  # Template read: Admin/TA/HR/RMG
    job = get_job_template(AUTH_DB_TARGET, jobId)
    if not job:
        return JSONResponse({"error": "Template not found."}, status_code=404)
    return {"job": job}


@app.delete("/job/config/{jobId}")
def job_config_delete(request: Request, jobId: str):
    _, auth_err = _require_user(request, {"hr", "manager", "admin"})
    if auth_err:
        return auth_err
    _enforce_crm_roles(request, "RMG")  # Template management: Admin/RMG
    ok = delete_job_template(AUTH_DB_TARGET, jobId)
    if not ok:
        return JSONResponse({"error": "Template not found."}, status_code=404)
    return {"status": "ok", "jobId": jobId}


@app.post("/ats/score")
async def ats_score_api(
    request: Request,
    jobId: str = Form(""),
    jobTitle: str = Form(""),
    requiredSkills: str = Form(""),
    optionalSkills: str = Form(""),
    expMin: int = Form(0),
    expMax: int = Form(0),
    domain: str = Form(""),
    jdText: str = Form(""),
    resumeText: str = Form(""),
    interviewAnswers: str = Form(""),
    weights: str = Form(""),
):
    _, auth_err = _require_interview_report_reader(request)
    if auth_err:
        return auth_err

    cfg = get_job_template(AUTH_DB_TARGET, jobId) if (jobId or "").strip() else None
    if cfg:
        jobTitle = cfg.get("jobTitle", jobTitle)
        requiredSkills = ", ".join(cfg.get("requiredSkills", []))
        optionalSkills = ", ".join(cfg.get("optionalSkills", []))
        expMin = int(cfg.get("expMin") or expMin or 0)
        expMax = int(cfg.get("expMax") or expMax or 0)
        domain = cfg.get("domain", domain)
        jdText = cfg.get("jdText", jdText)
        cfg_weights = cfg.get("weights") or {}
    else:
        cfg_weights = {}

    weights_obj = {}
    try:
        weights_obj = json.loads(weights) if (weights or "").strip() else {}
    except json.JSONDecodeError:
        weights_obj = {}
    merged_weights = {**cfg_weights, **weights_obj}
    w = AtsWeights.from_obj(merged_weights)

    answers = []
    raw_answers = (interviewAnswers or "").strip()
    if raw_answers:
        try:
            parsed = json.loads(raw_answers)
            if isinstance(parsed, list):
                answers = [str(x) for x in parsed if str(x).strip()]
            else:
                answers = [raw_answers]
        except json.JSONDecodeError:
            answers = [a.strip() for a in raw_answers.split("\n") if a.strip()]

    result = ats_score(
        jd_text=jdText,
        job_title=jobTitle,
        required_skills=[s.strip() for s in requiredSkills.split(",") if s.strip()],
        optional_skills=[s.strip() for s in optionalSkills.split(",") if s.strip()],
        resume_text=resumeText,
        interview_answers=answers,
        exp_min=expMin,
        exp_max=expMax,
        weights=w,
        domain=domain,
    )
    # Strict JSON output (already dict)
    return result


@app.post("/ats/score/upload")
async def ats_score_upload(
    request: Request,
    jd_file: UploadFile = File(...),
    cv_file: UploadFile = File(...),
    model: str = Form("gpt-4o-mini"),
):
    _, auth_err = _require_interview_report_reader(request)
    if auth_err:
        return auth_err
    jd_text = await _extract_text_from_upload(jd_file, model, False)
    if jd_text.startswith("__ERR__"):
        return {"error": jd_text.replace("__ERR__", "", 1)}
    cv_text = await _extract_text_from_upload(cv_file, model, False)
    if cv_text.startswith("__ERR__"):
        return {"error": cv_text.replace("__ERR__", "", 1)}

    # Prefer LLM scoring when OpenAI key is available; fallback to deterministic ATS if not.
    try:
        return ats_score_llm(jd_text=jd_text, resume_text=cv_text, model=model)
    except Exception as err:
        # Deterministic fallback (still produces structured output)
        return ats_score(
            jd_text=jd_text,
            job_title="",
            required_skills=[],
            optional_skills=[],
            resume_text=cv_text,
            interview_answers=[],
            exp_min=0,
            exp_max=0,
            weights=AtsWeights(),
            domain="",
        ) | {"meta": {"mode": "fallback", "error": str(err)}}


@app.get("/candidates/ranked")
def candidates_ranked(request: Request, jobId: str = "", limit: int = 200):
    _, auth_err = _require_interview_report_reader(request)
    if auth_err:
        return auth_err
    cfg = get_job_template(AUTH_DB_TARGET, jobId) if (jobId or "").strip() else None
    if not cfg:
        return {"error": "jobId is required. Configure a job first via /job/config."}
    w = AtsWeights.from_obj(cfg.get("weights") or {})

    records = load_hr_records(DATA_FILE)
    ranked = []
    for r in records[: max(1, min(int(limit or 200), 1000))]:
        answers = r.get("answers") if isinstance(r.get("answers"), list) else []
        # resume text may not be stored; use candidate profile + answers for now
        resume_text = json.dumps(r.get("candidate_profile") or {}, ensure_ascii=False)
        score = ats_score(
            jd_text=str(cfg.get("jdText") or ""),
            job_title=str(cfg.get("jobTitle") or ""),
            required_skills=list(cfg.get("requiredSkills") or []),
            optional_skills=list(cfg.get("optionalSkills") or []),
            resume_text=resume_text,
            interview_answers=[str(a) for a in answers],
            exp_min=int(cfg.get("expMin") or 0),
            exp_max=int(cfg.get("expMax") or 0),
            weights=w,
            domain=str(cfg.get("domain") or ""),
        )
        ranked.append(
            {
                "candidateId": r.get("id", ""),
                "candidate_name": r.get("candidate_name", "Candidate"),
                "candidate_email": r.get("candidate_email", "Not available"),
                "role": (r.get("candidate_profile") or {}).get("role_hint", "Candidate"),
                "atsScore": score.get("atsScore", 0),
                "grade": score.get("grade", ""),
                "hireProbability": score.get("hireProbability", ""),
                "missingSkills": score.get("missingSkills", []),
                "strongSkills": score.get("strongSkills", []),
            }
        )
    ranked.sort(key=lambda x: int(x.get("atsScore", 0)), reverse=True)
    return {"jobId": cfg.get("jobId", jobId), "ranked": ranked}


# ---------------------------
# Proctoring (strict)
# ---------------------------

PROCTOR_REPORT_FILE = DATA_DIR / "proctor_reports.json"
# In-memory proctor sessions — require UVICORN_WORKERS=1 (or Redis) for multi-instance.
_proctor_sessions: dict[str, dict] = {}
MAX_WARNINGS = 3
_INTEGRITY_VIOLATION_TYPES = frozenset({
    "tab_switch",
    "multiple_faces",
    "proctor_tabSwitch",
    "proctor_extraFace",
})


def _count_integrity_violations(events: list) -> int:
    return sum(
        1
        for event in events
        if isinstance(event, dict) and str(event.get("type") or "") in _INTEGRITY_VIOLATION_TYPES
    )


def _load_proctor_reports() -> dict:
    try:
        if PROCTOR_REPORT_FILE.exists():
            return json.loads(PROCTOR_REPORT_FILE.read_text(encoding="utf-8") or "{}") or {}
    except Exception:
        return {}
    return {}


def _save_proctor_reports(data: dict) -> None:
    try:
        PROCTOR_REPORT_FILE.parent.mkdir(parents=True, exist_ok=True)
        PROCTOR_REPORT_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        return


def _proctor_score(viol: dict) -> tuple[int, str, float, str]:
    tab = int((viol or {}).get("tabSwitch", 0) or 0)
    extra_face = int((viol or {}).get("extraFace", 0) or 0)
    total = tab + extra_face
    score = 100 - (total * 20)
    score = max(0, min(100, int(score)))
    # Three warning events are allowed; the 4th violation is a hard fail.
    if total > MAX_WARNINGS:
        status = "FAIL"
        risk = "High"
    elif total >= 3 or score < 80:
        status = "WARNING"
        risk = "Medium"
    else:
        status = "SAFE"
        risk = "Low"
    cheating_prob = round(min(0.99, max(0.01, total / 8.0)), 2)
    return score, status, cheating_prob, risk


def _parse_violations_log(raw) -> list:
    if isinstance(raw, list):
        return list(raw)
    if isinstance(raw, str) and raw.strip():
        try:
            v = json.loads(raw)
            return list(v) if isinstance(v, list) else []
        except Exception:
            return []
    return []


def _merge_proctor_events_into_schedule(invite_token: str, sess: dict) -> None:
    """Append live-proctor events + summary onto interview_schedule.violations_log for admin integrity."""
    token = (invite_token or "").strip()
    if not token or not sess:
        return
    rec = get_schedule_by_token(AUTH_DB_TARGET, token)
    if not rec:
        return
    prior = _parse_violations_log(rec.get("violations_log"))
    for evt in (sess.get("events") or [])[-80:]:
        etype = str((evt or {}).get("type") or "unknown").strip() or "unknown"
        if etype not in {"tabSwitch", "extraFace"}:
            continue
        prior.append({
            "type": f"proctor_{etype}",
            "details": str((evt or {}).get("meta") or "")[:500],
            "timestamp": str((evt or {}).get("at_ist") or ""),
            "ip": "",
            "user_agent": "",
        })
    violation_count = _count_integrity_violations(prior)
    update_schedule_field(
        AUTH_DB_TARGET,
        token,
        violation_count=violation_count,
        violations_log=json.dumps(prior, ensure_ascii=False),
    )


@app.post("/proctor/start-session")
async def proctor_start_session(
    request: Request,
    candidateId: str = Form(""),
    interviewId: str = Form(""),
):
    user, auth_err = _require_user(request, {"candidate", "hr"})
    if auth_err:
        return auth_err
    cid = (candidateId or user.get("username") or user.get("email") or "candidate").strip()
    iid = (interviewId or "").strip()
    sid = secrets.token_urlsafe(18)
    session = {
        "sessionId": sid,
        "candidateId": cid,
        "interviewId": iid,
        "started_at_ist": _now_ist_parts()["ist_iso"],
        "ended_at_ist": "",
        "violations": {"tabSwitch": 0, "extraFace": 0},
        "events": [],
        "status": "SAFE",
        "proctorScore": 100,
        "cheatingProbability": 0.01,
        "riskLevel": "Low",
        "terminated": False,
    }
    _proctor_sessions[sid] = session
    return {"status": "ok", "session": session}


@app.post("/proctor/violation")
async def proctor_violation(
    request: Request,
    sessionId: str = Form(""),
    type: str = Form(""),
    meta: str = Form(""),
):
    _, auth_err = _require_user(request, {"candidate", "hr"})
    if auth_err:
        return auth_err
    sid = (sessionId or "").strip()
    sess = _proctor_sessions.get(sid)
    if not sess:
        return {"error": "Invalid proctor session."}
    vtype = (type or "").strip()
    if vtype not in {"tabSwitch", "extraFace"}:
        vtype = "tabSwitch"
    if vtype not in sess["violations"]:
        sess["violations"][vtype] = 0
    sess["violations"][vtype] = int(sess["violations"].get(vtype, 0) or 0) + 1
    evt = {"type": vtype, "at_ist": _now_ist_parts()["ist_iso"], "meta": (meta or "")[:500]}
    sess["events"].append(evt)
    invite_tok = str(sess.get("invite_token") or sess.get("interviewId") or "").strip()
    if invite_tok:
        try:
            _merge_proctor_events_into_schedule(invite_tok, sess)
        except Exception as exc:
            logger.warning(
                "proctor.violation.schedule_persist_failed",
                extra={"event": "proctor.violation.schedule_persist_failed", "error": str(exc)},
            )
    score, status, prob, risk = _proctor_score(sess["violations"])
    sess["proctorScore"] = score
    sess["status"] = status
    sess["cheatingProbability"] = prob
    sess["riskLevel"] = risk
    terminate = status == "FAIL"
    if terminate:
        sess["terminated"] = True
    return {
        "violations": sess["violations"],
        "proctorScore": score,
        "status": status,
        "terminated": terminate,
        "warning_level": min(sum(int(v or 0) for v in sess["violations"].values()), MAX_WARNINGS + 1),
        "max_warnings": MAX_WARNINGS,
        "anomaly": {"riskLevel": risk, "cheatingProbability": prob},
    }


@app.post("/proctor/end-session")
async def proctor_end_session(
    request: Request,
    sessionId: str = Form(""),
    candidateId: str = Form(""),
):
    _, auth_err = _require_user(request, {"candidate", "hr"})
    if auth_err:
        return auth_err
    sid = (sessionId or "").strip()
    sess = _proctor_sessions.get(sid)
    if not sess:
        return {"error": "Invalid proctor session."}
    sess["ended_at_ist"] = _now_ist_parts()["ist_iso"]
    reports = _load_proctor_reports()
    cid = (candidateId or sess.get("candidateId") or "").strip() or "candidate"
    reports[cid] = sess
    _save_proctor_reports(reports)
    payload = _decode_token_from_header(request)
    invite_tok = str((payload or {}).get("invite_token") or "").strip()
    if invite_tok:
        try:
            _merge_proctor_events_into_schedule(invite_tok, sess)
        except Exception:
            logger.exception(
                "proctor.schedule_integrity_merge_failed",
                extra={"event": "proctor.schedule_integrity_merge_failed", "invite": invite_tok[:12]},
            )
    return {"status": "ok", "session": sess}


@app.get("/proctor/report/{candidate_id}")
def proctor_report(request: Request, candidate_id: str):
    _, auth_err = _require_user(request, {"hr"})
    if auth_err:
        return auth_err
    reports = _load_proctor_reports()
    rec = reports.get(str(candidate_id), None)
    if not rec:
        return {"error": "Report not found."}
    viol = rec.get("violations", {})
    score, status, prob, risk = _proctor_score(viol)
    return {
        "candidateId": candidate_id,
        "violations": viol,
        "proctorScore": score,
        "status": status,
        "anomaly": {"riskLevel": risk, "cheatingProbability": prob},
        "session": rec,
    }


@app.get("/hr-record/{record_id}")
def hr_record(request: Request, record_id: str):
    _, auth_err = _require_user(request, {"hr"})
    if auth_err:
        return auth_err
    # Prefer DB snapshot payload (admin + scheduled invite flows), fallback to file-based records.
    db_rec = get_interview_record_payload(AUTH_DB_TARGET, record_id)
    if isinstance(db_rec, dict) and db_rec:
        if "id" not in db_rec:
            db_rec["id"] = str(record_id)
        return {"record": db_rec}
    record = find_hr_record(load_hr_records(DATA_FILE), record_id)
    if record:
        return {"record": record}
    return {"error": "Record not found"}


@app.get("/hr-record/{record_id}/download")
def hr_record_download(request: Request, record_id: str, format: str = "json"):
    _, auth_err = _require_user(request, {"hr"})
    if auth_err:
        return auth_err
    record = get_interview_record_payload(AUTH_DB_TARGET, record_id)
    if not record:
        record = find_hr_record(load_hr_records(DATA_FILE), record_id)
    if not record:
        return {"error": "Record not found"}
    if "id" not in record:
        record["id"] = str(record_id)

    fmt = (format or "json").strip().lower()
    if fmt == "txt":
        lines = []
        lines.append(f"Interview ID: {record.get('id', '')}")
        lines.append(f"Candidate: {record.get('candidate_name', 'Candidate')}")
        lines.append(f"Email: {record.get('candidate_email', 'Not available')}")
        lines.append(f"Created At: {record.get('created_at', '')}")
        lines.append(f"Updated At: {record.get('updated_at', '')}")
        lines.append(f"Difficulty: {record.get('difficulty', '')}")
        lines.append(f"Model: {record.get('model', '')}")
        lines.append(f"Skills: {', '.join(record.get('skills', []))}")
        lines.append("")
        lines.append("Questions and Answers:")
        questions = record.get("questions", [])
        answers = record.get("answers", [])
        for idx, q in enumerate(questions):
            ans = answers[idx] if idx < len(answers) else ""
            lines.append(f"{idx + 1}. Q: {q}")
            lines.append(f"   A: {ans}")
        report = record.get("report")
        if report:
            lines.append("")
            lines.append("Evaluation:")
            lines.append(json.dumps(report, indent=2))
        return PlainTextResponse("\n".join(lines))

    if fmt == "xlsx":
        try:
            blob = build_evaluation_xlsx(record)
        except Exception as err:
            return JSONResponse(
                {"error": f"Excel build failed: {err}"},
                status_code=500,
            )
        fname = f"karnex-interview-{record_id[:8]}.xlsx"
        return Response(
            content=blob,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="{fname}"'},
        )

    return {"record": record}


@app.get("/session-status")
def session_status(request: Request):
    payload, auth_err = _require_user(request, {"hr", "candidate"})
    if auth_err:
        return auth_err
    sk = _session_key_from_payload(payload)
    s = sessions.get(sk)
    if not s:
        return {"active": False}
    return {
        "active": True,
        "completed": s["completed"],
        "submitted": s.get("submitted", False),
        "current": s["current"],
        "total": len(s["questions"]),
        "meta": s.get("meta", {}),
    }


@app.get("/health/live")
@app.get("/healthz", include_in_schema=False)
def health_live():
    return {"status": "live", "service": APP_TITLE}


@app.get("/health/ready")
@app.get("/readyz", include_in_schema=False)
def health_ready():
    checks = _runtime_core_checks()
    config = _runtime_config_checks()
    ready = all(checks.values())
    return JSONResponse(
        content={
            "status": "ready" if ready else "degraded",
            "service": APP_TITLE,
            "checks": checks,
            "config": config,
        },
        status_code=200 if ready else 503,
    )


@app.get("/admin/hr-code")
def admin_hr_code(request: Request):
    """Local-only endpoint for checking HR code status."""
    if not _is_local_request(request):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    code = _effective_report_code()
    return {"configured": bool(code), "length": len(code)}


@app.post("/admin/hr-code/rotate")
def admin_hr_code_rotate(request: Request, length: int = 8):
    """Local-only endpoint to rotate HR unlock code."""
    if not _is_local_request(request):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    length = max(6, min(length, 24))
    new_code = secrets.token_urlsafe(24).replace("-", "").replace("_", "")[:length]
    _write_report_code(new_code)
    return {"status": "ok", "message": "HR access code rotated and saved to local file."}


def _read_pdf_text(content: bytes) -> str:
    if not content:
        return ""
    reader = PdfReader(BytesIO(content))
    chunks = []
    for page in reader.pages:
        chunks.append(page.extract_text() or "")
    return "\n".join(chunks).strip()


async def _extract_text_from_upload(upload: UploadFile, model: str, safe_mode_on: bool) -> str:
    filename = (upload.filename or "").strip()
    ext = Path(filename).suffix.lower()
    content = await upload.read()
    content_type = (upload.content_type or "").lower()

    if not content:
        return "__ERR__Uploaded file is empty."

    if ext == ".pdf" or content_type == "application/pdf":
        text = _read_pdf_text(content)
        return text or "__ERR__Could not extract text from PDF."

    if ext == ".docx" or content_type in {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }:
        text = _read_docx_text(content)
        return text or "__ERR__Could not extract text from DOCX."

    if ext == ".doc" or content_type in {"application/msword"}:
        text = _read_binary_text_best_effort(content)
        if text:
            return text
        return (
            "__ERR__Legacy .doc extraction is limited. Please upload DOCX/PDF/TXT "
            "or enter Final Skills manually."
        )

    if content_type.startswith("image/") or ext in IMAGE_EXTENSIONS:
        if safe_mode_on:
            return "__ERR__Safe mode blocks image OCR. Please upload PDF/text or paste text."
        mime = content_type if content_type.startswith("image/") else "image/png"
        try:
            text = extract_text_from_image_bytes(content, mime_type=mime, model=model)
        except OpenAIError as err:
            return f"__ERR__Image OCR failed: {err}"
        except Exception:
            return "__ERR__Image OCR failed. Please try another image."
        return text or "__ERR__Could not extract text from image."

    if content_type.startswith("text/") or ext in TEXT_EXTENSIONS:
        try:
            return content.decode("utf-8").strip()
        except UnicodeDecodeError:
            return content.decode("latin-1", errors="ignore").strip()

    # Best-effort fallback: try extracting readable text from unknown file types.
    fallback_text = _read_binary_text_best_effort(content)
    if fallback_text:
        return fallback_text

    return (
        "__ERR__Could not extract readable text from this file type. "
        "Use PDF/image/text/DOCX or enter Final Skills manually."
    )


def _read_docx_text(content: bytes) -> str:
    try:
        with ZipFile(BytesIO(content)) as zf:
            xml_data = zf.read("word/document.xml")
    except (BadZipFile, KeyError, OSError):
        return ""

    try:
        root = ET.fromstring(xml_data)
    except ET.ParseError:
        return ""

    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    parts = []
    for node in root.findall(".//w:t", ns):
        if node.text:
            parts.append(node.text)
    return " ".join(parts).strip()


def _read_binary_text_best_effort(content: bytes) -> str:
    if not content:
        return ""
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        text = content.decode("latin-1", errors="ignore")
    # Keep mostly-readable text chunks to avoid binary garbage.
    cleaned = re.sub(r"[^\x09\x0A\x0D\x20-\x7E]", " ", text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if len(cleaned) < 40:
        return ""
    return cleaned


def _build_candidate_profile(
    extracted: dict,
    candidate_name: str = "",
    candidate_experience: str = "",
    candidate_email: str = "",
    candidate_role: str = "",
) -> dict:
    base = dict(extracted or {})
    base["name"] = (candidate_name or base.get("name") or "").strip()
    base["experience"] = (candidate_experience or base.get("experience") or "Not specified").strip()
    base["email"] = (candidate_email or base.get("email") or "Not available").strip()
    base["role_hint"] = (candidate_role or base.get("role_hint") or "Candidate").strip()
    return base


@app.get("/models")
def models(request: Request):
    if _is_production_env():
        _, auth_err = _require_user(request, {"hr", "admin", "manager", "candidate"})
        if auth_err:
            return auth_err
    base = (os.getenv("OPENAI_BASE_URL") or "").strip()
    return {
        "provider": "openai",
        "provider_base_url": base,
        "models": list(OPENAI_CHAT_MODELS),
    }


@app.post("/auth/register")
def auth_register(
    full_name: str = Form(""),
    email: str = Form(""),
    username: str = Form(""),
    password: str = Form(""),
    role: str = Form("candidate"),
):
    role_clean = str(role or "candidate").strip().lower()
    if role_clean == "hr" and not _allow_public_hr_registration():
        return JSONResponse(
            {"error": "HR self-registration is disabled. Contact an administrator."},
            status_code=403,
        )
    try:
        user = register_user(
            AUTH_DB_TARGET,
            full_name=full_name,
            email=email,
            username=username,
            password=password,
            role=role,
        )
        logger.info(
            "auth.register.success",
            extra={
                "event": "auth.register.success",
                "candidate_name": user.get("full_name", ""),
            },
        )
        return {"status": "ok", "user": user}
    except ValueError as err:
        return {"error": str(err)}


@app.post("/auth/login")
@_rl.limit("10/minute")
def auth_login(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
):
    result = verify_login(
        AUTH_DB_TARGET,
        username=username,
        password=password,
        client_ip=request.client.host if request.client else "unknown",
    )
    if not result.get("success"):
        return {"error": result.get("message", "Login failed.")}
    user = result.get("user", {})
    logger.info(
        "auth.login.success",
        extra={
            "event": "auth.login.success",
            "candidate_name": user.get("full_name", ""),
        },
    )
    token, expires_at_ist = _issue_access_token(user)
    return {
        "status": "ok",
        "user": user,
        "access_token": token,
        "token_type": "bearer",
        "expires_at_ist": expires_at_ist,
    }


@app.post("/auth/refresh")
@_rl.limit("30/minute")
def auth_refresh(request: Request):
    """Re-issue a fresh token for a STILL-VALID session (17 Aug 2026).

    Lets the dashboard offer "Stay signed in" shortly before expiry instead of
    dumping the user (and their half-typed form) onto the login page. An
    expired or missing token gets 401 — this never resurrects a dead session.
    Candidate interview sessions (invite_token claim) are excluded on purpose:
    those are device-bound and end when the interview window ends.
    """
    payload = _decode_token_from_header(request)
    if not payload:
        return JSONResponse(status_code=401, content={"error": "Session expired. Please login again."})
    if payload.get("invite_token"):
        return JSONResponse(status_code=403, content={"error": "Interview sessions cannot be extended."})
    user = {
        "username": payload.get("sub", ""),
        "role": payload.get("role", ""),
        "full_name": payload.get("full_name", ""),
        "email": payload.get("email", ""),
    }
    token, expires_at_ist = _issue_access_token(user)
    return {
        "status": "ok",
        "access_token": token,
        "token_type": "bearer",
        "expires_at_ist": expires_at_ist,
    }


@app.post("/auth/forgot-password")
@_rl.limit("5/minute")
def auth_forgot_password(
    request: Request,
    email: str = Form(""),
):
    """Start an email-based password reset. Always returns the same generic
    response so callers cannot enumerate which emails are registered."""
    generic = {"status": "ok", "message": "If that email exists, a reset link has been sent."}
    email_clean = (email or "").strip().lower()
    if not email_clean:
        return generic
    try:
        user = get_user_by_email(AUTH_DB_TARGET, email_clean)
        if not user:
            return generic
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        expires_at = (datetime.now(timezone.utc) + timedelta(minutes=45)).isoformat()
        create_password_reset(AUTH_DB_TARGET, int(user["id"]), token_hash, expires_at)
        base = _invite_base_url(request)
        reset_url = f"{base}/?reset_token={token}"
        if smtp_configured():
            mail_res = send_password_reset_email(email_clean, str(user.get("full_name") or ""), reset_url)
            if not mail_res.get("ok"):
                logger.warning("Password reset email send failed for %s: %s", email_clean, mail_res.get("error"))
                # NEVER log the token or the assembled reset URL: anyone with read
                # access to the log stream could take over the account with it.
                logger.error(
                    "auth.forgot_password.undelivered user_id=%s - reset token issued but not sent",
                    int(user["id"]),
                )
        else:
            logger.error(
                "auth.forgot_password.smtp_disabled user_id=%s - reset token issued but SMTP is off",
                int(user["id"]),
            )
        logger.info(
            "auth.forgot_password.issued",
            extra={"event": "auth.forgot_password.issued", "candidate_name": str(user.get("full_name") or "")},
        )
    except Exception as exc:  # never leak internals; response stays generic
        logger.warning("auth.forgot_password.error: %s", exc)
    return generic


@app.post("/auth/reset-password")
@_rl.limit("5/minute")
def auth_reset_password(
    request: Request,
    token: str = Form(""),
    new_password: str = Form(""),
):
    """Complete an email-based password reset using a single-use token."""
    token_clean = (token or "").strip()
    if not token_clean:
        return JSONResponse({"error": "Invalid or expired reset link"}, status_code=400)
    try:
        pwh.validate_password(new_password)
    except pwh.PasswordPolicyError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    token_hash = hashlib.sha256(token_clean.encode("utf-8")).hexdigest()
    row = consume_password_reset(AUTH_DB_TARGET, token_hash)
    if not row:
        return JSONResponse({"error": "Invalid or expired reset link"}, status_code=400)
    update_user_password(AUTH_DB_TARGET, int(row["user_id"]), pwh.hash_password(new_password))
    logger.info(
        "auth.password_reset.success",
        extra={"event": "auth.password_reset.success", "user_id": int(row["user_id"])},
    )
    return {"status": "ok", "message": "Password updated. Please sign in."}


@app.get("/auth/me")
def auth_me(request: Request):
    payload, auth_err = _require_user(request, {"hr", "candidate"})
    if auth_err:
        return auth_err
    return {
        "status": "ok",
        "user": {
            "username": payload.get("sub", ""),
            "role": payload.get("role", ""),
            "full_name": payload.get("full_name", ""),
            "email": payload.get("email", ""),
        },
    }


@app.post("/auth/logout")
def auth_logout():
    """Stateless JWT: client clears storage; endpoint exists for symmetry and future HttpOnly cookie clearing."""
    return {"status": "ok"}


@app.post("/hr/schedule-interview")
def hr_schedule_interview(
    request: Request,
    candidate_name: str = Form(""),
    candidate_email: str = Form(""),
    scheduled_at_local: str = Form(""),
    notes: str = Form(""),
    final_skills: str = Form(""),
    jobId: str = Form(""),
    num_q: int = Form(5),
    difficulty: str = Form("medium"),
    followup_mode: str = Form("false"),
    timing_mode: str = Form("count"),
    time_limit_sec: int = Form(0),
    mic_always_on: str = Form("false"),
    show_spoken_text: str = Form("false"),
    enable_transcript_input: str = Form(""),
    model: str = Form("gpt-4o-mini"),
    candidate_id: int = Form(0),      # optional Karnex CRM candidate id (Phase 5)
    opportunity_id: int = Form(0),    # optional Karnex CRM opportunity id (Phase 5)
):
    payload, auth_err = _require_user(request, {"hr"})
    if auth_err:
        return auth_err
    _enforce_crm_roles(request, "TA", "HR")  # HR Setup / scheduling: Admin/TA/HR
    skill_list = [s.strip().lower() for s in str(final_skills or "").split(",") if s.strip()]
    transcript_toggle_raw = enable_transcript_input if str(enable_transcript_input).strip() else show_spoken_text
    job_row = get_job_template(AUTH_DB_TARGET, str(jobId or "").strip()) if str(jobId or "").strip() else None
    qt_schedule = str((job_row or {}).get("questionType") or (job_row or {}).get("question_type") or "dynamic").strip().lower()
    mq_schedule = _normalized_manual_questions_for_job((job_row or {}).get("manualQuestions") if job_row else [])
    tm_schedule = str((job_row or {}).get("timingMode") or (job_row or {}).get("timing_mode") or timing_mode or "count").strip().lower()
    if qt_schedule == "manual" and mq_schedule:
        if tm_schedule == "count":
            num_q_packed = min(clamp_count_mode_questions(num_q), len(mq_schedule))
        else:
            num_q_packed = len(mq_schedule)
    else:
        num_q_packed = clamp_count_mode_questions(num_q)
    packed_notes = _pack_invite_config_into_notes(
        notes,
        {
            "final_skills": skill_list,
            "job_id": str(jobId or "").strip(),
            "num_q": num_q_packed,
            "difficulty": difficulty,
            "followup_mode": str(followup_mode).strip().lower() in {"1", "true", "yes", "on"},
            "timing_mode": timing_mode,
            "time_limit_sec": int(time_limit_sec or 0),
            "mic_always_on": str(mic_always_on).strip().lower() in {"1", "true", "yes", "on"},
            "show_spoken_text": str(transcript_toggle_raw).strip().lower() in {"1", "true", "yes", "on"},
            "enable_transcript_input": str(transcript_toggle_raw).strip().lower() in {"1", "true", "yes", "on"},
            "model": model,
        },
    )
    try:
        schedule = create_interview_schedule(
            AUTH_DB_TARGET,
            hr_username=str(payload.get("sub", "hr")),
            candidate_name=candidate_name,
            candidate_email=candidate_email,
            scheduled_at_local=scheduled_at_local,
            provider="karnex-link",
            meeting_link="",
            notes=packed_notes,
        )
    except ValueError as err:
        return {"error": str(err)}
    # Optional Karnex CRM linkage (Phase 5): scope this session's results to a
    # CRM candidate + opportunity. Best-effort; never blocks scheduling.
    if candidate_id and opportunity_id:
        try:
            from services.ai_interview_bridge import link_schedule_to_crm
            link_schedule_to_crm(
                str(schedule.get("invite_token", "")), str(schedule.get("id", "")),
                int(candidate_id), int(opportunity_id),
            )
        except Exception as _crm_link_exc:  # pragma: no cover
            logger.warning("CRM schedule linkage skipped: %s", _crm_link_exc)
    base = _invite_base_url(request)
    invite_url = f"{base}/?invite={schedule.get('invite_token', '')}"
    access_key = schedule.get("access_key", "")

    email_sent = False
    email_error: str | None = None
    ce = (candidate_email or "").strip()
    if ce:
        if smtp_configured():
            mail_res = send_interview_invite_email(
                ce,
                (candidate_name or "").strip() or "Candidate",
                invite_url,
                (scheduled_at_local or "").strip(),
                (notes or "").strip(),
                access_key=access_key,
            )
            email_sent = bool(mail_res.get("ok"))
            if not email_sent:
                email_error = str(mail_res.get("error") or "Send failed")
        else:
            email_error = "SMTP not configured (set SMTP_HOST, SMTP_USER, SMTP_PASSWORD in .env)."

    out: dict = {
        "status": "ok",
        "schedule": schedule,
        "invite_url": invite_url,
        "access_key": access_key,
        "email_sent": email_sent,
        "smtp_configured": smtp_configured(),
    }
    if email_error:
        out["email_error"] = email_error
    return out


@app.get("/hr/schedules")
def hr_schedules(request: Request):
    payload, auth_err = _require_user(request, {"hr"})
    if auth_err:
        return auth_err
    rows = list_interview_schedules(AUTH_DB_TARGET, str(payload.get("sub", "hr")))
    base = _invite_base_url(request)
    for row in rows:
        cfg = _extract_invite_config_from_notes(str(row.get("notes", "")))
        jid = str(cfg.get("job_id") or cfg.get("jobId") or "").strip()
        job = get_job_template(AUTH_DB_TARGET, jid) if jid else None
        job_title = str((job or {}).get("jobTitle") or "").strip()
        if jid:
            row["job_id"] = jid
        if job_title:
            row["job_title"] = job_title
            row["template_name"] = job_title
            row["role"] = job_title
        if job:
            row["opportunityId"] = str(job.get("opportunityId") or job.get("opportunity_id") or "").strip()
            row["customerName"] = str(job.get("customerName") or job.get("customer_name") or "").strip()
        token = str(row.get("invite_token") or "").strip()
        if token:
            row["invite_url"] = f"{base}/?invite={token}"
    return {"schedules": rows}


@app.delete("/hr/schedules/{schedule_id}")
def hr_schedule_delete(request: Request, schedule_id: str):
    payload, auth_err = _require_user(request, {"hr"})
    if auth_err:
        return auth_err
    ok = delete_interview_schedule(AUTH_DB_TARGET, schedule_id, str(payload.get("sub", "hr")))
    if not ok:
        return JSONResponse({"error": "Interview schedule not found."}, status_code=404)
    invalidate_integrity_logs_cache(str(payload.get("sub", "hr")))
    return {"status": "ok", "deleted": True, "schedule_id": schedule_id}


def _invite_closed_response(record: dict, invite_state: str, error_message: str) -> JSONResponse:
    """403 for invite when interview is finished or terminated; includes machine-readable state for the client UI."""
    name = str((record or {}).get("candidate_name") or "").strip()
    body: dict = {"error": error_message, "invite_state": invite_state}
    if name:
        body["candidate_name"] = name
    return JSONResponse(body, status_code=403)


@app.get("/candidate/invite/{token}")
def candidate_invite_lookup(token: str):
    record = get_schedule_by_token(AUTH_DB_TARGET, token)
    if not record:
        return JSONResponse({"error": "Invalid or expired interview link."}, status_code=404)
    session_status = str(record.get("session_status") or "pending").strip().lower()
    if session_status == "completed":
        return _invite_closed_response(
            record,
            "completed",
            "This interview has already been completed. The link is no longer valid.",
        )
    if session_status == "terminated":
        return _invite_closed_response(
            record,
            "terminated",
            "This interview was terminated due to policy violations. The link is no longer valid.",
        )
    access = _invite_access_state(record)
    _maybe_prewarm_invite_session(token, record, reason="lookup")
    prewarm = _invite_prewarm_snapshot(token)
    logger.info(
        "interview.invite.lookup",
        extra={
            "event": "interview.invite.lookup",
            "invite_token": _invite_token_tag(token),
            "session_status": session_status,
            "access_reason": str(access.get("reason") or ""),
            "seconds_until_start": int(access.get("seconds_until_start") or 0),
            "prewarm_status": str(prewarm.get("status") or "none"),
            "prewarm_latency_ms": int(prewarm.get("latency_ms") or 0),
        },
    )
    if access.get("reason") == "expired":
        return JSONResponse({"error": "This interview link has expired. Please contact HR for a new link."}, status_code=403)
    safe_schedule = {
        "candidate_name": record.get("candidate_name", ""),
        "scheduled_at_local": record.get("scheduled_at_local", ""),
        "status": record.get("status", ""),
        "session_status": session_status,
        "access_key": bool(record.get("access_key")),
    }
    return {"status": "ok", "schedule": safe_schedule, "access": access, "prewarm": prewarm}


@app.post("/candidate/invite/{token}/verify")
def candidate_invite_verify(token: str, request: Request, email: str = Form(""), access_key: str = Form("")):
    """Verify candidate email + access key before allowing interview entry."""
    record = get_schedule_by_token(AUTH_DB_TARGET, token)
    if not record:
        return JSONResponse({"error": "Invalid or expired interview link."}, status_code=404)

    max_attempts = 10
    attempts = increment_schedule_login_attempts(AUTH_DB_TARGET, token)
    if attempts > max_attempts:
        return JSONResponse({"error": "Too many verification attempts. This interview link has been locked. Please contact HR."}, status_code=403)

    access = _invite_access_state(record)
    if access.get("reason") == "expired":
        return JSONResponse({"error": "This interview link has expired. Please contact HR for a new link."}, status_code=403)

    stored_email = str(record.get("candidate_email", "")).strip().lower()
    stored_key = str(record.get("access_key", "")).strip().upper()

    submitted_email = (email or "").strip().lower()
    submitted_key = (access_key or "").strip().upper()

    if not submitted_email or not submitted_key:
        return JSONResponse({"error": "Email and access key are required."}, status_code=400)

    if submitted_email != stored_email:
        return JSONResponse({"error": "Email does not match the invited candidate."}, status_code=403)

    if not stored_key:
        pass
    elif not hmac.compare_digest(submitted_key, stored_key):  # constant-time secret compare
        return JSONResponse({"error": "Invalid access key. Please check the key shared by HR."}, status_code=403)

    session_status = str(record.get("session_status") or "pending").strip().lower()
    if session_status == "completed":
        return _invite_closed_response(
            record,
            "completed",
            "This interview has already been completed. The link is no longer valid.",
        )
    if session_status == "terminated":
        return _invite_closed_response(
            record,
            "terminated",
            "This interview was terminated due to policy violations. The link is no longer valid.",
        )
    if session_status == "active":
        request_device = str(request.headers.get("x-device-id") or "").strip()
        active_device = str(record.get("active_device_id") or "").strip()
        if not request_device or request_device != active_device:
            return JSONResponse({"error": "This interview session is already active on another device."}, status_code=403)

    now = _now_ist_parts()
    device_id = request.headers.get("x-device-id", "") or f"{request.client.host}_{now['ist_time']}"

    update_schedule_field(
        AUTH_DB_TARGET, token,
        verified_at=now["ist_iso"],
        session_status="verified",
        active_device_id=device_id,
    )

    _maybe_prewarm_invite_session(token, record, reason="verify")
    logger.info(
        "[SESSION] Verify accepted — prewarm triggered",
        extra={"event": "interview.invite.verify", "invite_token": _invite_token_tag(token)},
    )

    return {
        "status": "ok",
        "verified": True,
        "device_id": device_id,
        "candidate_name": record.get("candidate_name", "Candidate"),
        "access": access,
        "prewarm": _invite_prewarm_snapshot(token),
    }


@app.post("/candidate/invite/{token}/login")
def candidate_invite_login(token: str, request: Request):
    login_started = time.time()
    record = get_schedule_by_token(AUTH_DB_TARGET, token)
    if not record:
        return JSONResponse({"error": "Invalid or expired interview link."}, status_code=404)

    session_status = str(record.get("session_status") or "pending").strip().lower()
    if session_status == "completed":
        return _invite_closed_response(
            record,
            "completed",
            "This interview has already been completed. The link is no longer valid.",
        )
    if session_status == "terminated":
        return _invite_closed_response(
            record,
            "terminated",
            "This interview was terminated due to policy violations. The link is no longer valid.",
        )

    stored_key = str(record.get("access_key") or "").strip()
    if stored_key and session_status not in ("verified", "active"):
        return JSONResponse({"error": "Please verify your identity first."}, status_code=403)

    access = _invite_access_state(record)
    _maybe_prewarm_invite_session(token, record, reason="login")
    prewarm = _invite_prewarm_snapshot(token)
    logger.info(
        "interview.invite.login.start",
        extra={
            "event": "interview.invite.login.start",
            "invite_token": _invite_token_tag(token),
            "session_status": session_status,
            "access_reason": str(access.get("reason") or ""),
            "seconds_until_start": int(access.get("seconds_until_start") or 0),
            "prewarm_status": str(prewarm.get("status") or "none"),
            "prewarm_latency_ms": int(prewarm.get("latency_ms") or 0),
        },
    )
    if access.get("reason") == "expired":
        return JSONResponse({"error": "This interview link has expired. Please contact HR for a new link."}, status_code=403)
    if access.get("reason") == "scheduled_wait":
        logger.info(
            "interview.invite.login.waiting",
            extra={
                "event": "interview.invite.login.waiting",
                "invite_token": _invite_token_tag(token),
                "seconds_until_start": int(access.get("seconds_until_start", 0)),
                "prewarm_status": str(prewarm.get("status") or "none"),
                "prewarm_latency_ms": int(prewarm.get("latency_ms") or 0),
            },
        )
        return {
            "status": "scheduled_wait",
            "schedule": record,
            "seconds_until_start": int(access.get("seconds_until_start", 0)),
            "starts_at_ist": access.get("starts_at_ist", ""),
            "prewarm": prewarm,
        }

    device_id = request.headers.get("x-device-id", "")
    active_device = str(record.get("active_device_id") or "").strip()

    if session_status == "active":
        if active_device and device_id != active_device:
            return JSONResponse({"error": "This interview session is already active on another device."}, status_code=403)

    candidate_email = str(record.get("candidate_email", "")).strip().lower() or f"candidate-{token[:8]}@local"
    candidate_name = str(record.get("candidate_name", "Candidate")).strip() or "Candidate"
    skey = f"inv:{token}"
    wait_snap = _wait_for_invite_prewarm(token, timeout_sec=12.0)
    if sessions.get(skey):
        boot = {"status": "ok", "session_key": skey, "reused": True, "prewarm_wait": wait_snap}
        logger.info(
            "[SESSION] Login reused prewarmed session",
            extra={"event": "interview.invite.login.reused", "invite_token": _invite_token_tag(token)},
        )
    else:
        logger.info(
            "[SESSION] Fast bootstrap starting",
            extra={"event": "interview.invite.login.fast_bootstrap", "invite_token": _invite_token_tag(token)},
        )
        boot = _bootstrap_invite_interview_session(token, record, fast_only=True)
    if boot.get("error"):
        return JSONResponse({"error": boot["error"]}, status_code=400)

    now = _now_ist_parts()
    final_device_id = device_id or active_device or f"{request.client.host}_{now['ist_time']}"
    update_schedule_field(
        AUTH_DB_TARGET, token,
        session_status="active",
        interview_started_at=now["ist_iso"],
        active_device_id=final_device_id,
    )
    skey = f"inv:{token}"
    sess = sessions.get(skey)
    if sess:
        meta = sess.get("meta", {})
        meta["startup_login_accepted_at_utc"] = datetime.now(timezone.utc).isoformat()
        meta["startup_login_latency_ms"] = int((time.time() - login_started) * 1000)
        meta["startup_prewarm_status"] = str(prewarm.get("status") or "none")
        meta["startup_prewarm_latency_ms"] = int(prewarm.get("latency_ms") or 0)
        _persist_interview_progress(sess, status="in_progress")

    user = {
        "id": 0,
        "full_name": candidate_name,
        "email": candidate_email,
        "username": f"invite-{token[:10]}",
        "role": "candidate",
        "login_date_ist": now["ist_date"],
        "login_time_ist": now["ist_time"],
    }
    safe_schedule = {
        "candidate_name": record.get("candidate_name", ""),
        "scheduled_at_local": record.get("scheduled_at_local", ""),
    }
    token_value, expires_at_ist = _issue_access_token(user, {"invite_token": token})
    logger.info(
        "interview.invite.login.ready",
        extra={
            "event": "interview.invite.login.ready",
            "invite_token": _invite_token_tag(token),
            "latency_ms": int((time.time() - login_started) * 1000),
            "prewarm_status": str(prewarm.get("status") or "none"),
            "prewarm_latency_ms": int(prewarm.get("latency_ms") or 0),
            "boot_reused": bool(boot.get("reused")),
            "question_count": int(boot.get("question_count") or 0),
        },
    )
    return {
        "status": "ok",
        "user": user,
        "access_token": token_value,
        "token_type": "bearer",
        "expires_at_ist": expires_at_ist,
        "schedule": safe_schedule,
        "prewarm": prewarm,
        "boot_reused": bool(boot.get("reused")),
        "question_count": int(boot.get("question_count") or (len((sess or {}).get("questions") or []))),
        "login_latency_ms": int((time.time() - login_started) * 1000),
        "fast_bootstrap": bool(boot.get("fast_only")),
    }


@app.post("/interview/time-warning-audit")
def interview_time_warning_audit(request: Request, warning_key: str = Form("")):
    """Record that a timer warning banner was shown (troubleshooting audit)."""
    payload, auth_err = _require_user(request, {"candidate", "hr"})
    if auth_err:
        return auth_err
    key = str(warning_key or "").strip().lower()
    field = AUDIT_FIELD_BY_KEY.get(key)
    if not field:
        return {"status": "ignored", "reason": "unknown_warning_key"}
    sk = _session_key_from_payload(payload)
    s = sessions.get(sk)
    if not s:
        return {"status": "ignored", "reason": "no_session"}
    meta = s.setdefault("meta", {})
    audit = meta.setdefault("time_warning_audit", {})
    if audit.get(field):
        return {"status": "ok", "duplicate": True}
    now_iso = _now_ist_parts()["ist_iso"]
    audit[field] = now_iso
    _persist_interview_progress(s, status=_progress_status_for_session(s))
    return {"status": "ok", "field": field, "at": now_iso}


@app.post("/interview/violation")
def interview_violation(request: Request, violation_type: str = Form("tab_switch"), details: str = Form("")):
    """Log an anti-cheating violation from the candidate's browser."""
    payload, auth_err = _require_user(request, {"candidate", "hr"})
    if auth_err:
        return auth_err
    sk = _session_key_from_payload(payload)
    s = sessions.get(sk)
    if not s:
        return {"status": "ignored"}

    meta = s.get("meta", {})
    violations = meta.get("violations", [])
    now = _now_ist_parts()
    violations.append({
        "type": violation_type,
        "details": (details or "")[:500],
        "timestamp": now["ist_iso"],
        "ip": str(request.client.host) if request.client else "",
        "user_agent": str(request.headers.get("user-agent", ""))[:300],
    })
    meta["violations"] = violations
    violation_count = _count_integrity_violations(violations)
    meta["violation_count"] = violation_count

    invite_token = str(meta.get("invite_token", "")).strip()
    auto_terminated = False

    if violation_count > MAX_WARNINGS:
        # Mark policy termination immediately, but leave the in-memory session
        # answerable so the frontend can auto-save the current response before
        # the normal /submit finalization path removes the session.
        meta["termination_reason"] = "Repeated interview policy violations"
        meta["terminated_at"] = now["ist_iso"]
        violations.append({
            "type": "termination",
            "reason": "Repeated interview policy violations",
            "details": "Interview terminated due to repeated policy violations (tab switch / multiple faces)",
            "timestamp": now["ist_iso"],
        })
        meta["violations"] = violations
        auto_terminated = True
        if invite_token:
            update_schedule_field(
                AUTH_DB_TARGET, invite_token,
                session_status="terminated",
                interview_completed_at=now["ist_iso"],
                violation_count=violation_count,
                violations_log=json.dumps(violations, ensure_ascii=False),
            )
    elif invite_token:
        update_schedule_field(
            AUTH_DB_TARGET,
            invite_token,
            violation_count=violation_count,
            violations_log=json.dumps(violations, ensure_ascii=False),
        )

    invalidate_integrity_logs_cache()
    _persist_interview_progress(s, status="terminated" if auto_terminated else _progress_status_for_session(s))

    return {
        "status": "violation_logged",
        "violation_count": violation_count,
        "auto_terminated": auto_terminated,
        "warning_level": min(violation_count, MAX_WARNINGS + 1),
        "max_warnings": MAX_WARNINGS,
    }


_INTEGRITY_LOGS_CACHE: dict[str, tuple[float, dict]] = {}
_INTEGRITY_LOGS_CACHE_LOCK = threading.Lock()


def _integrity_logs_cache_ttl_s() -> int:
    try:
        return max(0, min(120, int(os.getenv("INTEGRITY_LOGS_CACHE_TTL_S", "20"))))
    except (TypeError, ValueError):
        return 20


def invalidate_integrity_logs_cache(hr_username: str | None = None) -> None:
    with _INTEGRITY_LOGS_CACHE_LOCK:
        if hr_username:
            _INTEGRITY_LOGS_CACHE.pop((hr_username or "hr").strip().lower(), None)
        else:
            _INTEGRITY_LOGS_CACHE.clear()


def _termination_reason_from_events(row: dict, events: list[dict]) -> str:
    for event in reversed(events):
        if not isinstance(event, dict):
            continue
        reason = str(event.get("reason") or event.get("termination_reason") or "").strip()
        if reason:
            return reason
    policy_count = _count_integrity_violations(events)
    if policy_count >= 4 or int(row.get("violation_count") or 0) >= 4:
        return "Repeated interview policy violations"
    if str(row.get("session_status") or "").strip().lower() == "terminated":
        return "Interview terminated"
    return ""


def _append_termination_event(row: dict, reason: str, now_iso: str) -> list[dict]:
    events = _parse_violations_log(row.get("violations_log"))
    if not any(isinstance(e, dict) and str(e.get("reason") or "") == reason for e in events):
        events.append(
            {
                "type": "termination",
                "reason": reason,
                "details": reason,
                "timestamp": now_iso,
            }
        )
    return events


def _cleanup_expired_integrity_rows(hr_user: str) -> None:
    """Lazy cleanup: expired pending/verified invites move from Integrity to Terminated."""
    now = datetime.now(IST)
    now_iso = now.isoformat()
    changed = False

    def _parse_istish(raw: str) -> datetime | None:
        text = str(raw or "").strip()
        if not text:
            return None
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
        if dt.tzinfo is None:
            return dt.replace(tzinfo=IST)
        return dt.astimezone(IST)

    def _stale_active_limit_seconds(row: dict) -> int:
        cfg = _extract_invite_config_from_notes(str(row.get("notes") or ""))
        try:
            cfg_limit = int(cfg.get("time_limit_sec") or 0)
        except (TypeError, ValueError):
            cfg_limit = 0
        # If no explicit time mode/limit was configured, default to 30 minutes
        # for stale-active cleanup so abandoned sessions do not stay active forever.
        base = cfg_limit if cfg_limit > 0 else 30 * 60
        # Keep the grace short. Long grace windows make completed interviews
        # appear "active" in Integrity after the candidate timer has expired.
        grace = 60
        return max(60, min(6 * 60 * 60, base + grace))

    def _active_reference_time(row: dict) -> datetime | None:
        # Prefer the actual start time. If it is missing due to an interrupted
        # login/start write, fall back to the scheduled time so active rows do
        # not remain stuck forever in Integrity.
        started_dt = _parse_istish(str(row.get("interview_started_at") or ""))
        if started_dt:
            return started_dt
        scheduled_dt = _parse_scheduled_local(str(row.get("scheduled_at_local") or ""))
        if scheduled_dt:
            return scheduled_dt
        return _parse_istish(str(row.get("created_at_ist") or ""))

    def _auto_finalize_stale_active_row(row: dict) -> bool:
        token = str(row.get("invite_token") or "").strip()
        if not token:
            return False
        skey = f"inv:{token}"
        sess = sessions.get(skey)
        if not sess:
            sess = _session_from_progress(get_interview_progress_by_invite(AUTH_DB_TARGET, token))
        if not sess:
            reason = "Active session timed out without final submission"
            events = _append_termination_event(row, reason, now_iso)
            update_schedule_field(
                AUTH_DB_TARGET,
                token,
                session_status="terminated",
                interview_completed_at=now_iso,
                violations_log=json.dumps(events, ensure_ascii=False),
            )
            logger.warning(
                "interview.invite.stale_active.no_session",
                extra={
                    "event": "interview.invite.stale_active.no_session",
                    "invite_token": _invite_token_tag(token),
                    "reason": reason,
                },
            )
            return True

        if not (sess.get("answers") or []):
            _append_pending_answer_on_submit(
                sess,
                "[Interview auto-closed after time limit/inactivity without a submitted response.]",
            )
        out = _finalize_interview_snapshot(sess, reason="stale_active_recovery", final_status="recovered")
        sessions.pop(skey, None)
        invalidate_hr_dashboard_cache()
        logger.info(
            "interview.invite.stale_active.autofinalized",
            extra={
                "event": "interview.invite.stale_active.autofinalized",
                "invite_token": _invite_token_tag(token),
                "answers_count": len(sess.get("answers") or []),
                "report_ready": bool(out.get("report_ready")),
            },
        )
        return True

    for row in list_interview_integrity_logs(AUTH_DB_TARGET, hr_user):
        status = str(row.get("session_status") or "pending").strip().lower()
        if status in {"completed", "terminated"}:
            continue
        if status == "active":
            active_ref_dt = _active_reference_time(row)
            if not active_ref_dt:
                continue
            if (now - active_ref_dt).total_seconds() > _stale_active_limit_seconds(row):
                if _auto_finalize_stale_active_row(row):
                    changed = True
            continue
        scheduled = _parse_scheduled_local(str(row.get("scheduled_at_local") or ""))
        if not scheduled or now <= scheduled + timedelta(hours=24):
            continue
        started = str(row.get("interview_started_at") or "").strip()
        verified = str(row.get("verified_at") or "").strip()
        if not started:
            reason = "Not attempted within 24 hours"
        elif not verified:
            reason = "Verification incomplete"
        else:
            reason = "Interview terminated"
        token = str(row.get("invite_token") or "").strip()
        if not token:
            continue
        events = _append_termination_event(row, reason, now_iso)
        update_schedule_field(
            AUTH_DB_TARGET,
            token,
            session_status="terminated",
            interview_completed_at=now_iso,
            violations_log=json.dumps(events, ensure_ascii=False),
        )
        changed = True
    if changed:
        invalidate_integrity_logs_cache()


@app.get("/interview/integrity-logs")
def interview_integrity_logs(request: Request):
    """Return integrity/violation data for admin panel."""
    _, auth_err = _require_user(request, {"hr"})
    if auth_err:
        return auth_err
    _enforce_crm_roles(request, "TA", "HR")  # Integrity: Admin/TA/HR (RMG excluded)
    payload = _decode_token_from_header(request)
    hr_user = str((payload or {}).get("sub", "hr")).strip().lower() or "hr"
    _recover_interviews_once(limit=50)
    _cleanup_expired_integrity_rows(hr_user)
    ttl = _integrity_logs_cache_ttl_s()
    if ttl > 0:
        with _INTEGRITY_LOGS_CACHE_LOCK:
            cached = _INTEGRITY_LOGS_CACHE.get(hr_user)
            if cached and (time.monotonic() - cached[0]) < ttl:
                return cached[1]
    rows = list_interview_integrity_logs(AUTH_DB_TARGET, hr_user)
    rows = _dedupe_integrity_schedule_rows(rows)
    logs = []
    terminated = []
    for full in rows:
        int(full.get("violation_count") or 0)
        events = _parse_violations_log(full.get("violations_log"))
        policy_violation_count = _count_integrity_violations(events)
        tab_switch_count = sum(
            1
            for event in events
            if isinstance(event, dict) and str(event.get("type") or "") in {"tab_switch", "proctor_tabSwitch"}
        )
        extra_face_count = sum(
            1
            for event in events
            if isinstance(event, dict) and str(event.get("type") or "") in {"multiple_faces", "proctor_extraFace"}
        )
        session_status = str(full.get("session_status") or "pending").strip().lower() or "pending"
        item = {
            "candidate_name": full.get("candidate_name", ""),
            "candidate_email": full.get("candidate_email", ""),
            "scheduled_at": full.get("scheduled_at_local", ""),
            "session_status": session_status,
            "login_attempts": int(full.get("login_attempts") or 0),
            "verified_at": full.get("verified_at", ""),
            "interview_started_at": full.get("interview_started_at", ""),
            "interview_completed_at": full.get("interview_completed_at", ""),
            "violation_count": policy_violation_count,
            "tab_switch_count": tab_switch_count,
            "extra_face_count": extra_face_count,
            "violations_log": [
                e
                for e in events
                if isinstance(e, dict)
                and str(e.get("type") or "") in {
                    "tab_switch",
                    "multiple_faces",
                    "proctor_tabSwitch",
                    "proctor_extraFace",
                    "termination",
                }
            ],
            "active_device_id": full.get("active_device_id", ""),
            "reason": _termination_reason_from_events(full, events),
            "template_name": "",
            "role": "",
            "terminated_at": full.get("interview_completed_at", ""),
        }
        cfg = _extract_invite_config_from_notes(str(full.get("notes", "")))
        jid = str(cfg.get("job_id") or cfg.get("jobId") or "").strip()
        job = get_job_template(AUTH_DB_TARGET, jid) if jid else None
        title = str((job or {}).get("jobTitle") or "").strip()
        item["template_name"] = title
        item["role"] = title
        if session_status == "terminated":
            terminated.append(item)
        else:
            logs.append(item)
    payload_out = {"logs": logs, "terminated": terminated}
    if ttl > 0:
        with _INTEGRITY_LOGS_CACHE_LOCK:
            _INTEGRITY_LOGS_CACHE[hr_user] = (time.monotonic(), payload_out)
    return payload_out


# ---------------------------------------------------------------------------
# AI Prompt Logs API (admin-only)
# ---------------------------------------------------------------------------

from routers import admin as admin_router

admin_router.configure(AUTH_DB_TARGET, _require_user)
app.include_router(admin_router.router)

# Question Bank API (admin dashboard) + template-wizard bank preview.
try:
    from routers import question_bank as question_bank_router

    app.include_router(question_bank_router.router)
    logger.info("Question Bank routes registered.")
except Exception as _qb_exc:  # pragma: no cover — never block boot
    logger.warning("Question Bank routes not registered: %s", _qb_exc)

# ---------------------------------------------------------------------------
# Karnex CRM API (Phase 3) — all /api/* CRM routers; see routers/crm/__init__.py
# CRM endpoints require PostgreSQL (CRM_DATABASE_URL / AUTH_DB_URL) and return
# 503 when unconfigured; the interview platform keeps working regardless.
# ---------------------------------------------------------------------------
try:
    from routers.crm import register_crm_routers

    register_crm_routers(app)
    logger.info("Karnex CRM routers registered.")
except Exception as _crm_exc:  # pragma: no cover — never block interview platform boot
    logger.error("Karnex CRM routers failed to register: %s", _crm_exc)

# Rate limiting is wired up right after `app = FastAPI(...)` above, because the
# @_rl.limit(...) decorators resolve the limiter at decoration time.


# Serve frontend from same backend server so one command runs all.
if FRONTEND_DIR.exists():
    admin_dist = _admin_dashboard_dist_path()
    if not _admin_dashboard_assets_ok():
        _try_build_admin_dashboard()
    if _admin_dashboard_assets_ok():
        app.mount("/admin", StaticFiles(directory=str(admin_dist), html=True), name="admin")
    else:

        @app.get("/admin", include_in_schema=False)
        @app.get("/admin/", include_in_schema=False)
        def admin_dashboard_unavailable() -> HTMLResponse:
            msg = (
                "<p>The admin UI is shipped as a static build under "
                "<code>frontend/admin-dashboard/dist/</code> (that folder is not in git).</p>"
                "<p><strong>Fix:</strong> install Node.js, then from the project root run "
                "<code>start_app.bat</code> (builds automatically), or run:</p>"
                "<pre>cd frontend/admin-dashboard\nnpm install\nnpm run build</pre>"
                "<p>Then restart the backend. Set <code>SKIP_ADMIN_DASHBOARD_BUILD=1</code> only if you "
                "intentionally skip the automatic build.</p>"
            )
            return HTMLResponse(
                content=(
                    "<!DOCTYPE html><html><head><meta charset=\"utf-8\"/>"
                    "<title>Admin dashboard unavailable</title></head><body>"
                    "<h1>Admin dashboard not built (503)</h1>"
                    f"{msg}"
                    "</body></html>"
                ),
                status_code=503,
            )

    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
else:
    pub_frontend = _public_frontend_base_url()

    if pub_frontend:

        @app.get("/", include_in_schema=False)
        async def root_redirect_to_frontend(request: Request) -> RedirectResponse:
            """API-only deploy: send browsers (invite links) to the Vercel frontend."""
            qs = request.url.query
            target = f"{pub_frontend}/" + (f"?{qs}" if qs else "")
            return RedirectResponse(target, status_code=302)
