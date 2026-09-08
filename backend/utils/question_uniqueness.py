from __future__ import annotations

import hashlib
import logging
import random
import re
import secrets
import time
from datetime import datetime, timezone
from typing import Iterable

logger = logging.getLogger(__name__)

DEFAULT_SIMILARITY_THRESHOLD = 0.7

_FILLER_RE = re.compile(
    r"\b(explain|describe|what|how|why|when|does|do|is|are|the|a|an|in|for|you|your|would|could|should|about|difference|between|purpose|role|use|using|tell|me|walk|through)\b",
    re.I,
)
_TOKEN_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9+#.]{2,}")
_CONCEPT_STOP = frozenset(
    {
        "android",
        "kotlin",
        "java",
        "mobile",
        "application",
        "component",
        "system",
        "interview",
        "question",
        "answer",
        "example",
        "examples",
    }
)


def make_question_session_seed(*parts: object) -> str:
    """Build a per-candidate question seed from stable IDs plus time/random entropy."""
    material = "\x1e".join(str(p or "") for p in parts)
    entropy = f"{time.time_ns()}\x1e{secrets.token_hex(12)}"
    return hashlib.sha256(f"{material}\x1e{entropy}".encode("utf-8")).hexdigest()


def _canonical_question(text: object) -> str:
    return " ".join(str(text or "").strip().lower().split())


def _keyword_set(text: str) -> set[str]:
    return set(_TOKEN_RE.findall((text or "").lower()))


def _clean_concept_token(token: str) -> str:
    t = (token or "").lower().strip(".,;:!?\"'()[]{}")
    return re.sub(r"^[^\w+#]+|[^\w+#]+$", "", t)


def extract_core_concepts(text: str) -> set[str]:
    """Topic tokens used to block paraphrased repeats (e.g. ViewModel variants)."""
    low = _FILLER_RE.sub(" ", (text or "").lower())
    low = re.sub(r"[^\w\s+#.]", " ", low)
    tokens = [_clean_concept_token(t) for t in low.split()]
    return {t for t in tokens if len(t) >= 3 and t not in _CONCEPT_STOP}


def question_similarity_score(a: str, b: str) -> float:
    """Semantic-ish similarity in [0, 1] using canonical text, keywords, and core concepts."""
    if not a or not b:
        return 0.0
    ca = _canonical_question(a)
    cb = _canonical_question(b)
    if not ca or not cb:
        return 0.0
    if ca == cb:
        return 1.0
    if len(ca) > 40 and len(cb) > 40 and ca[:40] == cb[:40]:
        return 0.95

    acon = extract_core_concepts(a)
    bcon = extract_core_concepts(b)
    if acon and bcon:
        shared = acon & bcon
        if not shared:
            return 0.0
        concept_score = len(shared) / (len(acon | bcon) or 1)
        if len(acon) <= 2 and len(bcon) <= 2:
            concept_score = max(concept_score, 0.88)
        return concept_score

    akw = _keyword_set(a)
    bkw = _keyword_set(b)
    return len(akw & bkw) / (len(akw | bkw) or 1) if akw and bkw else 0.0


def consecutive_concept_too_similar(new_q: str, prior: list[str] | None, *, threshold: float = DEFAULT_SIMILARITY_THRESHOLD) -> bool:
    """True when new_q repeats the same concept as the immediately prior served question."""
    if not prior:
        return False
    last = str(prior[-1] or "").strip()
    n = (new_q or "").strip()
    if not last or not n or len(n) < 12:
        return False
    return question_similarity_score(n, last) >= threshold


def question_too_similar(new_q: str, prior: list[str] | None, *, threshold: float = DEFAULT_SIMILARITY_THRESHOLD) -> bool:
    """True when new_q is an exact or semantic duplicate of any prior question."""
    n = (new_q or "").strip()
    if not prior:
        return False
    if len(n) < 12:
        return False
    if consecutive_concept_too_similar(n, prior, threshold=threshold):
        return True
    for old in prior or []:
        if not old:
            continue
        if question_similarity_score(n, str(old)) >= threshold:
            return True
    return False


def dedupe_question_list_semantic(questions: Iterable[object], *, threshold: float = DEFAULT_SIMILARITY_THRESHOLD) -> list[str]:
    """Filter a batch keeping only semantically unique questions."""
    out: list[str] = []
    for raw in questions or []:
        q = " ".join(str(raw or "").strip().split())
        if not q:
            continue
        if question_too_similar(q, out, threshold=threshold):
            logger.info("[DYNAMIC] Duplicate Question Detected", extra={"question": q[:180]})
            continue
        out.append(q)
    return out


def prepare_unique_question_sequence(
    questions: Iterable[object],
    *,
    seed: str,
    asked_questions: Iterable[object] | None = None,
    limit: int | None = None,
) -> list[str]:
    """
    Deduplicate (exact + semantic) and deterministically shuffle a session's question order.
    """
    asked = {_canonical_question(q) for q in (asked_questions or []) if _canonical_question(q)}
    seen = set(asked)
    unique: list[str] = []
    for raw in questions or []:
        q = " ".join(str(raw or "").strip().split())
        key = _canonical_question(q)
        if not q or not key or key in seen:
            continue
        if question_too_similar(q, unique):
            logger.info("[DYNAMIC] Duplicate Question Detected", extra={"question": q[:180]})
            continue
        seen.add(key)
        unique.append(q)

    seed_int = int.from_bytes(hashlib.sha256(str(seed or "").encode("utf-8")).digest()[:8], "big")
    rng = random.Random(seed_int)
    rng.shuffle(unique)
    if limit is not None:
        return unique[: max(0, int(limit))]
    return unique


def remember_asked_question(session: dict, question: object) -> None:
    """Track served questions in session metadata without duplicating repeats."""
    meta = session.setdefault("meta", {})
    asked = meta.setdefault("asked_questions", [])
    key = _canonical_question(question)
    if not key:
        return
    if key not in {_canonical_question(q) for q in asked}:
        asked.append(" ".join(str(question or "").strip().split()))


def record_question_registry(
    session: dict,
    *,
    question_number: int,
    question_text: str,
    status: str,
    source: str = "dynamic",
) -> None:
    """Persist per-question audit row in session meta (checkpointed to interview_progress)."""
    meta = session.setdefault("meta", {})
    reg = meta.setdefault("question_registry", [])
    if not isinstance(reg, list):
        reg = []
        meta["question_registry"] = reg
    now = datetime.now(timezone.utc).isoformat()
    q = " ".join(str(question_text or "").strip().split())
    if not q:
        return
    for row in reg:
        if not isinstance(row, dict):
            continue
        if int(row.get("question_number") or 0) == int(question_number) and _canonical_question(row.get("question_text")) == _canonical_question(q):
            row["status"] = status
            row["updated_at_utc"] = now
            return
    reg.append(
        {
            "interview_id": str(meta.get("interview_id") or ""),
            "question_number": int(question_number),
            "question_text": q[:900],
            "status": str(status or "generated")[:32],
            "source": str(source or "dynamic")[:32],
            "recorded_at_utc": now,
        }
    )


def record_generated_questions_batch(session: dict, questions: list[str], *, source: str = "dynamic") -> None:
    for i, q in enumerate(questions or [], start=1):
        record_question_registry(session, question_number=i, question_text=q, status="generated", source=source)
        logger.info("[DYNAMIC] Generated Question", extra={"n": i, "question": str(q)[:180]})


def session_prior_questions(session: dict, *, include_future: bool = False) -> list[str]:
    meta = session.get("meta", {}) or {}
    cur = int(session.get("current") or 0)
    qs = list(session.get("questions") or [])
    prior = list(meta.get("asked_questions") or [])
    prior.extend(qs[:cur])
    if include_future:
        prior.extend(qs[cur:])
    out: list[str] = []
    seen: set[str] = set()
    for q in prior:
        key = _canonical_question(q)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(" ".join(str(q or "").strip().split()))
    return out


def regenerate_unique_fallback_question(session: dict, *, avoid: list[str] | None = None, max_attempts: int = 8) -> str:
    """Generate a single non-duplicate fallback question for dynamic interviews."""
    from ai import generate_questions_fallback

    meta = session.get("meta", {}) or {}
    jd = str(meta.get("jd_text") or "")
    skills = list(meta.get("jd_skills") or [])
    diff = str(meta.get("session_difficulty") or meta.get("difficulty") or "medium")
    avoid_list = list(avoid or []) + session_prior_questions(session, include_future=True)
    for attempt in range(max(1, max_attempts)):
        batch = generate_questions_fallback(jd, "", diff, 4, required_skills=skills or None)
        for q in batch or []:
            t = " ".join(str(q or "").strip().split())
            if not t or question_too_similar(t, avoid_list):
                logger.info(
                    "[DYNAMIC] Duplicate Question Detected",
                    extra={"attempt": attempt + 1, "question": t[:180]},
                )
                continue
            logger.info("[DYNAMIC] Regenerating Question", extra={"attempt": attempt + 1, "question": t[:180]})
            return t
    return ""


def ensure_unique_served_question(session: dict) -> bool:
    """
    If the question about to be served duplicates a prior turn, replace it (dynamic mode only).
    Returns True when the current question was replaced.
    """
    meta = session.get("meta", {}) or {}
    prior = session_prior_questions(session, include_future=False)
    if not prior:
        return False
    cur = int(session.get("current") or 0)
    qs = session.get("questions") or []
    if cur >= len(qs):
        return False
    current_q = " ".join(str(qs[cur] or "").strip().split())
    if not current_q:
        return False
    if not question_too_similar(current_q, prior):
        return False

    logger.info("[DYNAMIC] Duplicate Question Detected", extra={"question": current_q[:180], "index": cur + 1})
    replacement = regenerate_unique_fallback_question(session, avoid=prior + [current_q])
    if not replacement:
        return False
    qs[cur] = replacement
    session["questions"] = qs
    record_question_registry(
        session,
        question_number=cur + 1,
        question_text=replacement,
        status="regenerated",
        source=str(meta.get("question_source") or "dynamic"),
    )
    logger.info("[DYNAMIC] Regenerating Question", extra={"index": cur + 1, "question": replacement[:180]})
    return True


def build_question_avoid_history(
    *,
    global_recent: Iterable[object] | None = None,
    job_recent: Iterable[object] | None = None,
    manual_questions: Iterable[object] | None = None,
    template_preview: Iterable[object] | None = None,
    session_asked: Iterable[object] | None = None,
    limit: int = 120,
) -> list[str]:
    """
    Merge anti-repetition sources for OpenAI question generation.

    Order: template manual lines → saved preview → prior interviews on this job
    → global recent turns → current session asked list.
    """
    out: list[str] = []
    seen: set[str] = set()

    def _add(items: Iterable[object] | None) -> None:
        for raw in items or []:
            q = " ".join(str(raw or "").strip().split())
            key = _canonical_question(q)
            if not q or not key or key in seen:
                continue
            seen.add(key)
            out.append(q)
            if len(out) >= limit:
                return

    _add(manual_questions)
    _add(template_preview)
    _add(job_recent)
    _add(global_recent)
    _add(session_asked)
    return out[:limit]
