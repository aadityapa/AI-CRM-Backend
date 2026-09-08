from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

# Imported lazily-safe: utils.warmup only depends on stdlib so this module
# stays cheap to import inside fastapi startup paths.
from ai import align_qa_to_answered_turns
from utils.warmup import extract_and_strip_intro, filter_out_warmups


def build_hr_records_summary(records: list[dict]) -> list[dict]:
    summary = [
        {
            "id": r.get("id", ""),
            "candidate_name": r.get("candidate_name", "Candidate"),
            "candidate_email": r.get("candidate_email", "Not available"),
            "created_at": r.get("created_at", ""),
            "updated_at": r.get("updated_at", ""),
            "created_date_ist": r.get("created_date_ist", ""),
            "created_time_ist": r.get("created_time_ist", ""),
            "updated_date_ist": r.get("updated_date_ist", ""),
            "updated_time_ist": r.get("updated_time_ist", ""),
            "submitted": bool(r.get("submitted")),
            "has_report": bool(r.get("report")),
            "final_status": r.get("final_status", ""),
            "report_status": r.get("report_status", "ready" if r.get("report") else "pending"),
        }
        for r in records
    ]
    summary.sort(key=lambda x: x.get("updated_at", ""), reverse=True)
    return summary


def find_hr_record(records: list[dict], record_id: str) -> dict | None:
    for record in records:
        if str(record.get("id", "")) == str(record_id):
            return record
    return None


def build_submitted_record(session: dict, updated_ist: dict) -> dict:
    meta = session.get("meta", {})
    job_id = str(meta.get("job_id") or "").strip()
    job_title = str(meta.get("job_title") or "").strip()
    # Issue 2 (May 2026): exclude the non-evaluated warmup Q/A from HR-facing
    # records so reviewers never see "Please introduce yourself." with the
    # candidate's small-talk answer alongside the scored questions.
    rec_q, rec_a = filter_out_warmups(session.get("questions"), session.get("answers"), meta)
    rec_q, rec_a, _ = align_qa_to_answered_turns(rec_q, rec_a)
    return {
        "id": meta.get("interview_id", str(uuid4())),
        "created_at": meta.get("created_at"),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "created_at_ist": meta.get("created_at_ist"),
        "created_date_ist": meta.get("created_date_ist"),
        "created_time_ist": meta.get("created_time_ist"),
        "updated_at_ist": updated_ist["ist_iso"],
        "updated_date_ist": updated_ist["ist_date"],
        "updated_time_ist": updated_ist["ist_time"],
        "candidate_name": (meta.get("candidate_profile", {}) or {}).get("name", "Candidate"),
        "candidate_email": (meta.get("candidate_profile", {}) or {}).get("email", "Not available"),
        "candidate_profile": meta.get("candidate_profile", {}),
        "difficulty": meta.get("difficulty", "Medium"),
        "model": meta.get("model", "gpt-4o-mini"),
        "skills": meta.get("jd_skills", []),
        "job_id": job_id,
        "job_title": job_title,
        "invite_token": meta.get("invite_token", ""),
        "scheduled_at_local": meta.get("scheduled_at_local", ""),
        "questions": rec_q,
        "answers": rec_a,
        "submitted": True,
        "final_status": meta.get("final_status", "completed"),
        "finalization_reason": meta.get("finalization_reason", ""),
        "report_status": "pending",
        "report": None,
    }


def build_report_record(session: dict, report_result: dict, evaluated_ist: dict) -> dict:
    meta = session.get("meta", {})
    job_id = str(meta.get("job_id") or "").strip()
    job_title = str(meta.get("job_title") or "").strip()
    # Realign the scored per-question rows with the warmup-filtered transcript and
    # surface the introduction turn for display (fixes the first technical question
    # inheriting the intro's 0% and shows what the candidate said in the warmup).
    extract_and_strip_intro(report_result, session.get("questions"), session.get("answers"), meta)
    rec_q, rec_a = filter_out_warmups(session.get("questions"), session.get("answers"), meta)
    rec_q, rec_a, _ = align_qa_to_answered_turns(rec_q, rec_a)
    # Persist whether this role was assessed on communication at all. The report
    # is read long after the session is gone, so the flag has to live in the
    # record — otherwise the UI cannot tell "communication scored 0" apart from
    # "communication was never assessed", and would show an empty card either way.
    if isinstance(report_result, dict):
        report_result.setdefault(
            "communication_required", bool(meta.get("communication_required", True))
        )
    return {
        "id": meta.get("interview_id", str(uuid4())),
        "created_at": meta.get("created_at"),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "created_at_ist": meta.get("created_at_ist"),
        "created_date_ist": meta.get("created_date_ist"),
        "created_time_ist": meta.get("created_time_ist"),
        "updated_at_ist": evaluated_ist["ist_iso"],
        "updated_date_ist": evaluated_ist["ist_date"],
        "updated_time_ist": evaluated_ist["ist_time"],
        "candidate_name": (meta.get("candidate_profile", {}) or {}).get("name", "Candidate"),
        "candidate_email": (meta.get("candidate_profile", {}) or {}).get("email", "Not available"),
        "candidate_profile": meta.get("candidate_profile", {}),
        "difficulty": meta.get("difficulty", "Medium"),
        "model": meta.get("model", "gpt-4o-mini"),
        "skills": meta.get("jd_skills", []),
        "job_id": job_id,
        "job_title": job_title,
        "invite_token": meta.get("invite_token", ""),
        "scheduled_at_local": meta.get("scheduled_at_local", ""),
        "questions": rec_q,
        "answers": rec_a,
        "submitted": bool(session.get("submitted")),
        "final_status": meta.get("final_status", "completed"),
        "finalization_reason": meta.get("finalization_reason", ""),
        "report_status": "ready",
        "report": report_result,
    }

