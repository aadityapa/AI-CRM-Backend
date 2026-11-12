"""One candidate name everywhere (12 Nov 2026).

Reported: TA uploads a candidate (single upload or bulk ZIP), the name comes out
wrong, TA fixes it with Edit applicant — and the Candidates master keeps the old
name, so the profile, the Screening Desk, Talent Pool and every other position
the candidate is in still show it.

The rule now:
* the Candidate master holds THE name (`first_name` + `middle_name` + `last_name`);
* every `resumes.candidate_name` of that candidate is a copy of it;
* a name changed anywhere (Edit applicant on a position, the Candidates page,
  the profile) goes through `rename_candidate`, which writes the master and
  every copy in one go and logs it on each of the candidate's profiles;
* an upload for a candidate who is ALREADY in the master keeps the master's name
  (a bulk ZIP name is read off the CV and must not overwrite a name TA fixed);
  only a placeholder master name ("Unknown", blank) takes the uploaded one.

`tidy_name` cleans what parsers and file names produce: "RAVI_KUMAR_Resume" →
"Ravi Kumar". Pure; the DB helpers flush, the caller commits.
"""
from __future__ import annotations

import logging
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

#: Words a file name or a CV header adds to a name ("Ravi Kumar Resume 2026").
_NOISE_WORDS = {"resume", "cv", "biodata", "profile", "updated", "final", "latest",
                "new", "copy", "pdf", "docx", "doc"}
#: Master names that are not really a name — an upload may replace them.
PLACEHOLDER_NAMES = {"", "unknown", "candidate", "na", "n/a", "none", "null", "-"}
NAME_MAX = 120


def tidy_name(raw: str | None) -> str:
    """Clean a typed / parsed name. Never returns blank for a non-blank input
    that has at least one real word."""
    text = str(raw or "")
    text = re.sub(r"[_\t\r\n]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" .,-|:;")
    if not text:
        return ""
    words = [w for w in text.split(" ") if w]
    kept = [w for w in words
            if w.strip(".()[]").lower() not in _NOISE_WORDS and not re.fullmatch(r"[\d()\[\].-]+", w)]
    if kept:
        words = kept
    out = " ".join(words)
    # ALL CAPS or all lower → Title Case ("RAVI KUMAR" → "Ravi Kumar"); a name
    # already in mixed case (McKenzie, D'Souza) is left as typed.
    letters = [c for c in out if c.isalpha()]
    if letters and (all(c.isupper() for c in letters) or all(c.islower() for c in letters)):
        out = " ".join(_title_word(w) for w in out.split(" "))
    return out


def _title_word(word: str) -> str:
    # "d'souza" → "D'Souza", "anne-marie" → "Anne-Marie"; str.title() also does
    # this but turns "o'brien's" oddly — fine for names.
    return word[:1].upper() + word[1:].lower() if len(word) <= 2 else word.title()


def split_name(full: str) -> tuple[str, str | None]:
    """First word = first name, the rest = last name (middle name left empty —
    most screens print first + last, so a middle name there would vanish)."""
    parts = (full or "").strip().split(None, 1)
    if not parts:
        return "Unknown", None
    return parts[0][:NAME_MAX], (parts[1][:NAME_MAX] if len(parts) > 1 else None)


def full_name(candidate) -> str:
    return " ".join(p for p in (getattr(candidate, "first_name", None),
                                getattr(candidate, "middle_name", None),
                                getattr(candidate, "last_name", None)) if p).strip()


def same_name(a: str | None, b: str | None) -> bool:
    norm = lambda s: re.sub(r"\s+", " ", str(s or "")).strip().lower()  # noqa: E731
    return norm(a) == norm(b)


def is_placeholder(candidate) -> bool:
    return full_name(candidate).strip().lower() in PLACEHOLDER_NAMES


def sync_resume_names(db: Session, candidate) -> int:
    """Copy the master's name onto every resume of the candidate. Returns how
    many rows changed."""
    from models import Resume

    name = full_name(candidate)
    if not name or candidate.id is None:
        return 0
    changed = 0
    for r in db.execute(select(Resume).where(Resume.candidate_id == candidate.id)).scalars():
        if r.candidate_name != name:
            r.candidate_name = name[:255]
            changed += 1
    return changed


def _log_on_profiles(db: Session, candidate, old: str, new: str, user_id: int | None, where: str) -> None:
    from models import CandidateProfile, CandidateProfileActivityLog
    from services.crm_common import log_activity

    try:
        with db.begin_nested():
            ids = db.execute(select(CandidateProfile.id).where(
                CandidateProfile.candidate_id == candidate.id)).scalars().all()
            for pid in ids:
                log_activity(db, CandidateProfileActivityLog, "profile_id", pid, user_id,
                             "NAME_CHANGED", f"Name changed from “{old}” to “{new}” ({where})")
    except Exception:  # noqa: BLE001 — the log never blocks the rename
        logger.warning("candidate_name_log_failed for candidate %s", candidate.id, exc_info=True)


def rename_candidate(db: Session, candidate, new_name: str | None, *, user_id: int | None = None,
                     where: str = "edited", parts: tuple | None = None) -> dict:
    """Give the candidate a new name everywhere. Returns
    `{changed, old, new, resumes}`; nothing happens for a blank or unchanged name.
    `parts` = (first, middle, last) as typed on the Candidates form — kept as
    split there; otherwise the full name is split first / rest."""
    old = full_name(candidate)
    if parts:
        first, middle, last = (tidy_name(p) or None for p in parts)
        if not first:
            return {"changed": False, "old": old, "new": old, "resumes": 0}
        new = " ".join(p for p in (first, middle, last) if p)
    else:
        new = tidy_name(new_name)
        first, last = split_name(new) if new else ("", None)
        middle = None
    # Exact compare: a case-only fix ("RAVI KUMAR" → "Ravi Kumar") is a change too.
    if not new or new == old:
        return {"changed": False, "old": old, "new": old, "resumes": 0}
    candidate.first_name, candidate.middle_name, candidate.last_name = first[:NAME_MAX], \
        (middle[:NAME_MAX] if middle else None), (last[:NAME_MAX] if last else None)
    db.flush()
    resumes = sync_resume_names(db, candidate)
    _log_on_profiles(db, candidate, old, full_name(candidate), user_id, where)
    return {"changed": True, "old": old, "new": full_name(candidate), "resumes": resumes}


def settle_upload_name(db: Session, candidate, resume, *, created: bool) -> None:
    """After an upload is matched to a candidate: a NEW candidate takes the tidied
    upload name; an EXISTING one keeps the master's name (and the resume row
    shows it), unless the master's name is only a placeholder."""
    typed = tidy_name(getattr(resume, "candidate_name", None))
    if typed and (created or is_placeholder(candidate)):
        first, last = split_name(typed)
        candidate.first_name, candidate.middle_name, candidate.last_name = first, None, last
        if not created:
            sync_resume_names(db, candidate)
    name = full_name(candidate)
    if name:
        resume.candidate_name = name[:255]
