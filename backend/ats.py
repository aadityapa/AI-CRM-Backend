from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openai_client import get_openai_client, openai_key_configured
from prompt_logger import tracked_chat_completion, log_openai_call

from paths import DATA_DIR


def _client():
    # ATS has its own purpose key (falls back to eval, then to the master key)
    # so a scan-all burst cannot exhaust the quota a live interview depends on.
    return get_openai_client("ats")


def _db_target() -> str:
    direct = (os.getenv("AUTH_DB_URL") or "").strip()
    if direct:
        return direct
    host = (os.getenv("DB_HOST") or "").strip()
    port = (os.getenv("DB_PORT") or "5432").strip()
    name = (os.getenv("DB_NAME") or "").strip()
    user = (os.getenv("DB_USER") or "").strip()
    pw = (os.getenv("DB_PASSWORD") or "").strip()
    if host and name and user:
        return f"postgresql://{user}:{pw}@{host}:{port}/{name}"
    from paths import KARNEX_DB_FILE
    return str(KARNEX_DB_FILE)


ATS_CACHE_FILE = DATA_DIR / "ats_cache.json"
JOB_CONFIG_FILE = DATA_DIR / "job_configs.json"


def _load_json(path: Path, default):
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8") or "null") or default
    except Exception:
        return default
    return default


def _save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _stable_hash(payload: dict) -> str:
    dumped = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(dumped.encode("utf-8")).hexdigest()


def _normalize_skill_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        items = value
    else:
        items = re.split(r"[,;\n]", str(value))
    out: list[str] = []
    seen: set[str] = set()
    for raw in items:
        s = str(raw or "").strip()
        if not s:
            continue
        low = s.lower()
        if low in seen:
            continue
        seen.add(low)
        out.append(s)
    return out


def _extract_years(text: str) -> int | None:
    t = text or ""
    # 5+ years, 5 yrs, 5 years of experience
    m = re.search(r"(\d{1,2})\s*\+?\s*(?:years|year|yrs|yr)\b", t, re.IGNORECASE)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            return None
    # "experience: 7" or "7 years experience"
    m2 = re.search(r"\bexperience\b[^0-9]{0,10}(\d{1,2})\b", t, re.IGNORECASE)
    if m2:
        try:
            return int(m2.group(1))
        except ValueError:
            return None
    return None


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for i in range(len(a)):
        dot += a[i] * b[i]
        na += a[i] * a[i]
        nb += b[i] * b[i]
    denom = (na**0.5) * (nb**0.5)
    if denom <= 0:
        return 0.0
    return dot / denom


_ROLE_CATEGORIES: dict[str, list[str]] = {
    "frontend": ["react", "angular", "vue", "typescript", "javascript", "css", "html", "ui", "ux"],
    "backend": ["java", "spring", "fastapi", "django", "node", "microservices", "api", "sql", "postgres", "redis"],
    "qa": ["testing", "selenium", "cypress", "playwright", "istqb", "junit", "pytest"],
    "devops": ["docker", "kubernetes", "ci/cd", "jenkins", "terraform", "aws", "azure", "gcp"],
    "ai": ["machine learning", "deep learning", "nlp", "llm", "pytorch", "tensorflow", "ml"],
    "embedded": ["autosar", "can", "can bus", "rtos", "c++", "automotive ethernet", "simulink"],
}


def _guess_role_category(job_title: str, jd_text: str, required_skills: list[str]) -> str:
    blob = f"{job_title}\n{jd_text}\n{', '.join(required_skills)}".lower()
    best = ("backend", 0)
    for cat, keys in _ROLE_CATEGORIES.items():
        hits = sum(1 for k in keys if k in blob)
        if hits > best[1]:
            best = (cat, hits)
    return best[0]


def _extract_education(text: str) -> tuple[list[str], list[str]]:
    t = (text or "").lower()
    degrees: list[str] = []
    certs: list[str] = []
    degree_terms = [
        ("bachelor", ["b.tech", "be", "b.e", "bsc", "b.sc", "bachelor"]),
        ("master", ["m.tech", "me", "m.e", "msc", "m.sc", "master"]),
        ("phd", ["phd", "doctorate"]),
    ]
    for label, needles in degree_terms:
        if any(n in t for n in needles):
            degrees.append(label)
    cert_terms = [
        "aws",
        "azure",
        "gcp",
        "istqb",
        "cka",
        "ckad",
        "ocpjp",
        "oracle",
        "scrum",
        "pmp",
    ]
    for c in cert_terms:
        if re.search(rf"(?<![a-z0-9]){re.escape(c)}(?![a-z0-9])", t):
            certs.append(c.upper() if c in {"aws", "gcp"} else c.upper())
    return degrees[:3], sorted(list(set(certs)))[:10]


def _quality_signal_from_answers(answers: list[str]) -> float:
    text = " ".join([a.strip() for a in (answers or []) if a and a.strip()])
    if not text:
        return 0.0
    words = re.findall(r"[A-Za-z0-9+#.]{2,}", text)
    uniq = len(set(w.lower() for w in words))
    total = len(words)
    diversity = (uniq / max(total, 1)) if total else 0.0
    has_metrics = 1.0 if re.search(r"\b(ms|s|sec|%|percent|latency|throughput|qps|tps|rpm)\b", text, re.IGNORECASE) else 0.0
    has_incident = 1.0 if re.search(r"\b(root cause|postmortem|incident|outage|debug)\b", text, re.IGNORECASE) else 0.0
    length_score = min(1.0, total / 220.0)  # saturates around ~220 words
    return max(0.0, min(1.0, 0.45 * length_score + 0.25 * diversity + 0.15 * has_metrics + 0.15 * has_incident))


def _points(weight: int, ratio: float) -> int:
    r = max(0.0, min(1.0, ratio))
    return int(round(weight * r))


@dataclass(frozen=True)
class AtsWeights:
    keyword: int = 40
    relevance: int = 25
    experience: int = 20
    education: int = 10
    behavior: int = 5

    @staticmethod
    def from_obj(obj: dict | None) -> "AtsWeights":
        o = obj or {}
        def clamp(x, default):
            try:
                n = int(x)
            except Exception:
                n = default
            return max(0, min(n, 100))

        w = AtsWeights(
            keyword=clamp(o.get("keywordMatch"), 40),
            relevance=clamp(o.get("skillRelevance"), 25),
            experience=clamp(o.get("experienceMatch"), 20),
            education=clamp(o.get("educationMatch"), 10),
            behavior=clamp(o.get("behaviorScore"), 5),
        )
        total = w.keyword + w.relevance + w.experience + w.education + w.behavior
        if total != 100 and total > 0:
            # Normalize to 100 while preserving proportions.
            scale = 100.0 / float(total)
            kw = int(round(w.keyword * scale))
            rel = int(round(w.relevance * scale))
            exp = int(round(w.experience * scale))
            edu = int(round(w.education * scale))
            beh = 100 - (kw + rel + exp + edu)
            return AtsWeights(keyword=kw, relevance=rel, experience=exp, education=edu, behavior=beh)
        return w


def _grade(score: int) -> str:
    s = int(score)
    if s >= 95:
        return "A+"
    if s >= 90:
        return "A"
    if s >= 85:
        return "A-"
    if s >= 80:
        return "B+"
    if s >= 75:
        return "B"
    if s >= 70:
        return "B-"
    if s >= 65:
        return "C+"
    if s >= 60:
        return "C"
    return "D"


def _hire_probability(score: int) -> str:
    if score >= 85:
        return "High"
    if score >= 70:
        return "Medium"
    return "Low"


def save_job_config(job: dict) -> dict:
    data = _load_json(JOB_CONFIG_FILE, default={"jobs": {}})
    jobs = data.get("jobs", {}) if isinstance(data, dict) else {}
    job_id = str(job.get("jobId") or job.get("id") or "").strip() or hashlib.md5(
        f"{job.get('jobTitle','')}".encode("utf-8")
    ).hexdigest()[:10]
    job_out = {
        "jobId": job_id,
        "jobTitle": str(job.get("jobTitle") or "").strip(),
        "requiredSkills": _normalize_skill_list(job.get("requiredSkills")),
        "optionalSkills": _normalize_skill_list(job.get("optionalSkills")),
        "expMin": int(job.get("expMin") or 0),
        "expMax": int(job.get("expMax") or 0),
        "weights": job.get("weights") or {},
        "domain": str(job.get("domain") or "").strip(),
        "jdText": str(job.get("jdText") or "").strip(),
    }
    jobs[job_id] = job_out
    _save_json(JOB_CONFIG_FILE, {"jobs": jobs})
    return job_out


def load_job_config(job_id: str) -> dict | None:
    data = _load_json(JOB_CONFIG_FILE, default={"jobs": {}})
    jobs = data.get("jobs", {}) if isinstance(data, dict) else {}
    item = jobs.get(str(job_id), None)
    return item if isinstance(item, dict) else None


def list_job_configs() -> list[dict]:
    data = _load_json(JOB_CONFIG_FILE, default={"jobs": {}})
    jobs = data.get("jobs", {}) if isinstance(data, dict) else {}
    out = []
    for v in jobs.values():
        if isinstance(v, dict):
            out.append(v)
    out.sort(key=lambda x: (x.get("jobTitle") or "").lower())
    return out


def delete_job_config(job_id: str) -> bool:
    jid = str(job_id or "").strip()
    if not jid:
        return False
    data = _load_json(JOB_CONFIG_FILE, default={"jobs": {}})
    jobs = data.get("jobs", {}) if isinstance(data, dict) else {}
    if jid not in jobs:
        return False
    try:
        del jobs[jid]
        _save_json(JOB_CONFIG_FILE, {"jobs": jobs})
        return True
    except Exception:
        return False


def ats_score(
    *,
    jd_text: str,
    job_title: str,
    required_skills: list[str],
    optional_skills: list[str],
    resume_text: str,
    interview_answers: list[str],
    exp_min: int,
    exp_max: int,
    weights: AtsWeights,
    domain: str = "",
    embedding_model: str = "text-embedding-3-small",
) -> dict:
    req = [s.strip() for s in (required_skills or []) if s and s.strip()]
    opt = [s.strip() for s in (optional_skills or []) if s and s.strip()]
    doc = "\n".join([jd_text or "", resume_text or "", "\n".join(interview_answers or [])]).strip()

    cache_key = _stable_hash(
        {
            "v": 1,
            "jd_text": jd_text,
            "job_title": job_title,
            "required_skills": req,
            "optional_skills": opt,
            "resume_text": resume_text,
            "answers": interview_answers,
            "exp_min": exp_min,
            "exp_max": exp_max,
            "domain": domain,
            "weights": weights.__dict__,
            "embedding_model": embedding_model,
        }
    )
    cache = _load_json(ATS_CACHE_FILE, default={"scores": {}})
    scores = cache.get("scores", {}) if isinstance(cache, dict) else {}
    if cache_key in scores:
        return scores[cache_key]

    text_lower = doc.lower()
    (jd_text or "").lower()

    # Keyword matching: exact hits in resume+answers, weighted by required skills.
    matched_exact: list[str] = []
    missing: list[str] = []
    for s in req:
        token = s.lower()
        if re.search(rf"(?<![a-z0-9]){re.escape(token)}(?![a-z0-9])", text_lower):
            matched_exact.append(s)
        else:
            missing.append(s)

    # Semantic matching (AI): embeddings similarity for skills not exactly found.
    sem_hits: list[str] = []
    sem_scores: dict[str, float] = {}
    if missing and doc:
        import time as _time
        _emb_start = _time.perf_counter()
        _emb_status = "success"
        _emb_error = ""
        try:
            emb_doc = (
                _client()
                .embeddings.create(model=embedding_model, input=doc[:7000])
                .data[0]
                .embedding
            )
            emb_sk = _client().embeddings.create(model=embedding_model, input=missing[:60]).data
            for idx, s in enumerate(missing):
                vec = emb_sk[idx].embedding if idx < len(emb_sk) else []
                sim = _cosine(vec, emb_doc)
                sem_scores[s] = sim
                if sim >= 0.53:
                    sem_hits.append(s)
        except Exception as _exc:
            sem_scores = {}
            sem_hits = []
            _emb_status = "failed"
            _emb_error = str(_exc)
        finally:
            _emb_elapsed = int((_time.perf_counter() - _emb_start) * 1000)
            log_openai_call(
                db_target=_db_target(),
                call_type="ats_embedding",
                model=embedding_model,
                messages=[{"role": "user", "content": f"[embedding] skills: {', '.join(missing[:12])}"}],
                response_time_ms=_emb_elapsed,
                status=_emb_status,
                error_log=_emb_error,
            )

    matched_all = matched_exact + [s for s in sem_hits if s not in matched_exact]
    missing_final = [s for s in req if s not in matched_all]

    keyword_ratio = (len(matched_all) / max(len(req), 1)) if req else 0.0
    keyword_points = _points(weights.keyword, keyword_ratio)

    # Skill relevance: compare skill set to inferred role category, plus optional skills bonus.
    role_cat = _guess_role_category(job_title, jd_text, req)
    cat_keys = _ROLE_CATEGORIES.get(role_cat, [])
    cat_hits = 0
    for s in matched_all:
        low = s.lower()
        if any(k in low for k in cat_keys):
            cat_hits += 1
    relevance_ratio = (cat_hits / max(len(req), 1)) if req else 0.0
    opt_bonus = 0.0
    if opt:
        opt_hit = sum(
            1
            for s in opt
            if re.search(rf"(?<![a-z0-9]){re.escape(s.lower())}(?![a-z0-9])", text_lower)
        )
        opt_bonus = min(0.15, opt_hit / max(len(opt), 1) * 0.15)
    relevance_points = _points(weights.relevance, min(1.0, relevance_ratio + opt_bonus))

    # Experience matching: parse years from resume/interview and compare to required range.
    yrs = _extract_years(resume_text) or _extract_years("\n".join(interview_answers or [])) or _extract_years(jd_text)
    exp_ratio = 0.0
    if yrs is not None:
        lo = max(0, int(exp_min or 0))
        hi = max(lo, int(exp_max or lo))
        if hi == 0:
            exp_ratio = 0.75
        else:
            if yrs < lo:
                exp_ratio = max(0.0, yrs / max(lo, 1))
            elif yrs > hi:
                exp_ratio = max(0.65, 1.0 - ((yrs - hi) / max(hi + 5, 1)) * 0.35)
            else:
                exp_ratio = 1.0
    experience_points = _points(weights.experience, exp_ratio)

    # Education & certifications: simple deterministic scan.
    degrees, certs = _extract_education(resume_text)
    edu_ratio = 0.0
    if degrees:
        edu_ratio += 0.65
    if certs:
        edu_ratio += 0.35
    education_points = _points(weights.education, min(1.0, edu_ratio))

    # Interview performance signals: deterministic heuristic from answers.
    behavior_ratio = _quality_signal_from_answers(interview_answers or [])
    behavior_points = _points(weights.behavior, behavior_ratio)

    total = keyword_points + relevance_points + experience_points + education_points + behavior_points
    total = max(0, min(100, int(total)))

    strong = matched_all[:8]
    recommendation = "Strong match" if total >= 85 else "Moderate match" if total >= 70 else "Weak match"
    if missing_final[:3]:
        recommendation += f" but improve {', '.join(missing_final[:3])}"
    if role_cat:
        recommendation += f" for {role_cat.title()} role"

    out = {
        "atsScore": total,
        "grade": _grade(total),
        "breakdown": {
            "keywordMatch": keyword_points,
            "skillRelevance": relevance_points,
            "experienceMatch": experience_points,
            "educationMatch": education_points,
            "behaviorScore": behavior_points,
        },
        "missingSkills": missing_final[:12],
        "strongSkills": strong,
        "recommendation": recommendation,
        "hireProbability": _hire_probability(total),
        # extra explainability (frontend can hide)
        "meta": {
            "roleCategory": role_cat,
            "yearsDetected": yrs,
            "semanticScores": {k: round(v, 3) for k, v in sem_scores.items() if k in missing[:10]},
            "matchedExact": matched_exact[:12],
            "matchedSemantic": sem_hits[:12],
        },
    }

    scores[cache_key] = out
    _save_json(ATS_CACHE_FILE, {"scores": scores})
    return out


def ats_score_llm(*, jd_text: str, resume_text: str, model: str = "gpt-4o-mini") -> dict:
    """
    LLM-assisted ATS scoring (best-effort).
    Returns the same top-level keys as ats_score().
    """
    if not openai_key_configured("ats"):
        raise RuntimeError(
            "No OpenAI key configured for ATS. Set OPENAI_API_KEY_ATS, "
            "OPENAI_API_KEY_EVALUATION or OPENAI_API_KEY."
        )
    jd = (jd_text or "").strip()
    cv = (resume_text or "").strip()
    if not jd or not cv:
        raise ValueError("Both JD text and resume text are required.")

    client = _client()
    prompt = {
        "role": "user",
        "content": (
            "You are an ATS engine for hiring.\n"
            "Score the candidate against the JD with maximum precision.\n"
            "Return ONLY strict JSON with this schema:\n"
            "{\n"
            '  "atsScore": number (0-100),\n'
            '  "grade": string,\n'
            '  "hireProbability": "High"|"Medium"|"Low",\n'
            '  "strongSkills": string[],\n'
            '  "missingSkills": string[],\n'
            '  "recommendation": string,\n'
            '  "breakdown": {\n'
            '     "keywordMatch": number,\n'
            '     "skillRelevance": number,\n'
            '     "experienceMatch": number,\n'
            '     "educationMatch": number,\n'
            '     "behaviorScore": number\n'
            "  }\n"
            "}\n\n"
            "Rules:\n"
            "- atsScore must be an integer 0..100.\n"
            "- strongSkills/missingSkills: max 12 items each.\n"
            "- Keep recommendation short and actionable.\n\n"
            "JOB DESCRIPTION:\n"
            f"{jd}\n\n"
            "CANDIDATE CV/RESUME:\n"
            f"{cv}\n"
        ),
    }
    sys = {
        "role": "system",
        "content": "You are a strict JSON generator. No markdown. No commentary. Output JSON only.",
    }
    msgs = [sys, prompt]
    resp = tracked_chat_completion(
        client,
        model=model,
        messages=msgs,
        temperature=0.1,
        call_type="ats_score_llm",
        db_target=_db_target(),
    )
    text = ""
    try:
        text = str(resp.choices[0].message.content or "").strip()
    except Exception:
        text = ""
    try:
        parsed = json.loads(text)
    except Exception:
        # Fallback: try to extract the first JSON object.
        m = re.search(r"\{[\s\S]*\}", text)
        parsed = json.loads(m.group(0)) if m else {}

    score = int(parsed.get("atsScore", 0) or 0)
    score = max(0, min(100, score))
    breakdown = parsed.get("breakdown", {}) if isinstance(parsed.get("breakdown", {}), dict) else {}
    out = {
        "atsScore": score,
        "grade": str(parsed.get("grade") or _grade(score)),
        "hireProbability": str(parsed.get("hireProbability") or _hire_probability(score)),
        "strongSkills": [str(x) for x in (parsed.get("strongSkills") or [])][:12] if isinstance(parsed.get("strongSkills"), list) else [],
        "missingSkills": [str(x) for x in (parsed.get("missingSkills") or [])][:12] if isinstance(parsed.get("missingSkills"), list) else [],
        "recommendation": str(parsed.get("recommendation") or ""),
        "breakdown": {
            "keywordMatch": int(breakdown.get("keywordMatch") or 0),
            "skillRelevance": int(breakdown.get("skillRelevance") or 0),
            "experienceMatch": int(breakdown.get("experienceMatch") or 0),
            "educationMatch": int(breakdown.get("educationMatch") or 0),
            "behaviorScore": int(breakdown.get("behaviorScore") or 0),
        },
        "meta": {"mode": "llm", "model": model},
    }
    return out

