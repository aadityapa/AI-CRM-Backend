"""Skill-wise question bank for the AI interview (10 Oct 2026).

A position template can carry its OWN questions, written by RMG / GM per skill
(HR does not author templates), in three kinds:

* ``normal``  — spoken question + the expected answer. Asked aloud like today.
* ``pseudo``  — a pseudo-code snippet + 2-6 options + the correct option, and
  optionally "partly right" options that earn partial credit. Shown on screen
  only (no voice); the candidate clicks an option.
* ``coding``  — an easy coding problem + a reference solution. Shown on screen
  only; the candidate types code in an editor.

At the start the candidate ticks the skills they have worked on (the JD's
mandatory skills are ticked and locked) and ONLY questions of those skills are
asked. The AI only checks the answers (pseudo-code is marked without any model
call), which is what makes it cheaper than a generated interview. The one
exception (11 Oct 2026, user decision): when a picked skill has fewer spoken
questions than ``normal_per_skill``, ONE model call writes the missing ones at
medium level (``ai_fill``, on by default) — see ``skill_bank_fill``.

Order (11 Oct 2026): theory first, the pseudo-code and coding questions placed
IN BETWEEN the spoken ones, never first and never last (``interleave``).

This module is PURE: no I/O, no model calls. ``main.py`` stores the config in
``job_templates.weights["skillBank"]`` and the per-question details in the
session's ``meta["question_specs"]`` keyed by session index (string).
"""
from __future__ import annotations

import hashlib
import random
import re
import uuid
from typing import Any, Iterable

KIND_NORMAL = "normal"
KIND_PSEUDO = "pseudo"
KIND_CODING = "coding"
KINDS = (KIND_NORMAL, KIND_PSEUDO, KIND_CODING)
#: Shown on screen only — never spoken, never listened to.
TYPED_KINDS = frozenset({KIND_PSEUDO, KIND_CODING})

QUESTION_SOURCE = "skill_bank"
WEIGHTS_KEY = "skillBank"

MAX_QUESTIONS = 400
MAX_TEXT = 2000
MAX_EXPECTED = 4000
MAX_CODE = 6000
MAX_OPTION = 400
MIN_OPTIONS = 2
MAX_OPTIONS = 6
MAX_SKILL = 80
#: Coding languages a question may allow (12 Oct 2026: automotive + Android hiring —
#: Embedded C, CAPL, MATLAB, Kotlin…). The candidate picks one of the question's
#: allowed languages in the editor; the AI judges the code in THAT language.
CODE_LANGUAGES = ("c", "embedded_c", "cpp", "java", "kotlin", "python", "javascript", "typescript",
                  "csharp", "go", "rust", "swift", "sql", "shell", "capl", "matlab", "asm")
LANGUAGES = CODE_LANGUAGES + ("pseudo", "any")
LANGUAGE_LABELS = {
    "c": "C", "embedded_c": "Embedded C (C for microcontrollers: registers, ISRs, fixed-width types)",
    "cpp": "C++", "java": "Java", "kotlin": "Kotlin (Android)", "python": "Python",
    "javascript": "JavaScript", "typescript": "TypeScript", "csharp": "C#", "go": "Go", "rust": "Rust",
    "swift": "Swift", "sql": "SQL", "shell": "Shell / Bash", "capl": "CAPL (Vector CANoe / CANalyzer)",
    "matlab": "MATLAB", "asm": "Assembly (ARM / embedded)", "pseudo": "pseudo-code", "any": "any language",
}
MAX_STARTERS = len(CODE_LANGUAGES)

#: The user's numbers (10 Oct 2026): one coding, two pseudo-code, the rest normal.
DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "coding_count": 1,
    "pseudo_count": 2,
    "normal_per_skill": 2,
    "coding_minutes": 10,
    "pseudo_minutes": 2,
    "pick_min": 2,
    "pick_max": 5,
    "ai_fill": True,
    # How many SPOKEN questions one interview asks in total (12 Oct 2026, "the
    # template decides how many spoken, pseudo-code and coding"). 0 = the older
    # rule: up to `normal_per_skill` for every picked skill.
    "spoken_count": 0,
}
#: Rough minutes one spoken question takes (question read aloud + answer).
SPOKEN_MINUTES_EACH = 2.5
#: The level AI-written top-up questions are asked at (user decision 11 Oct 2026).
AI_FILL_LEVEL = "medium"
#: A "partly right" pseudo-code option earns this share of the marks by default.
DEFAULT_PARTIAL_CREDIT = 50
PARTIAL_CREDIT_RANGE = (10, 90)
_LIMITS = {
    "coding_count": (0, 10),
    "pseudo_count": (0, 20),
    # "Our questions" (11 Oct 2026) asks every question RMG / GM wrote — a
    # skill may hold many of them.
    "normal_per_skill": (0, 50),
    "spoken_count": (0, 200),
    "coding_minutes": (2, 45),
    "pseudo_minutes": (1, 10),
    "pick_min": (1, 20),
    "pick_max": (1, 20),
}


class SkillBankError(ValueError):
    """A template's question bank is malformed — the message names what is wrong."""


# ---------------------------------------------------------------------------
# Template config
# ---------------------------------------------------------------------------

def _int(value: Any, default: int, lo: int, hi: int) -> int:
    try:
        n = int(float(value))
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def _text(value: Any, limit: int) -> str:
    return str(value or "").replace("\r\n", "\n").strip()[:limit]


def norm_skill(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()[:MAX_SKILL]


def skill_key(value: Any) -> str:
    return norm_skill(value).lower()


def clean_question(raw: Any, position: int) -> dict:
    """One bank question, validated. Raises SkillBankError naming the question."""
    if not isinstance(raw, dict):
        raise SkillBankError(f"Question {position}: not a question.")
    kind = str(raw.get("kind") or KIND_NORMAL).strip().lower()
    if kind not in KINDS:
        raise SkillBankError(f"Question {position}: unknown kind '{kind}'.")
    skill = norm_skill(raw.get("skill"))
    if not skill:
        raise SkillBankError(f"Question {position}: choose the skill it belongs to.")
    text = _text(raw.get("text"), MAX_TEXT)
    if not text:
        raise SkillBankError(f"Question {position} ({skill}): the question is empty.")
    qid = re.sub(r"[^A-Za-z0-9_-]", "", str(raw.get("id") or ""))[:40] or uuid.uuid4().hex[:12]
    out: dict[str, Any] = {"id": qid, "skill": skill, "kind": kind, "text": text}
    language = str(raw.get("language") or "").strip().lower()
    if kind == KIND_NORMAL:
        out["expected"] = _text(raw.get("expected"), MAX_EXPECTED)
    elif kind == KIND_PSEUDO:
        code = _text(raw.get("code"), MAX_CODE)
        if not code:
            raise SkillBankError(f"Question {position} ({skill}): add the pseudo-code snippet.")
        options = [_text(o, MAX_OPTION) for o in (raw.get("options") or []) if _text(o, MAX_OPTION)]
        if len(options) < MIN_OPTIONS:
            raise SkillBankError(f"Question {position} ({skill}): a pseudo-code question needs at least "
                                 f"{MIN_OPTIONS} options.")
        options = options[:MAX_OPTIONS]
        correct = _int(raw.get("correct"), -1, -1, len(options) - 1)
        if correct < 0:
            raise SkillBankError(f"Question {position} ({skill}): mark the correct option.")
        partial: list[int] = []
        for v in raw.get("partial") or []:
            i = _int(v, -1, -1, 10 ** 6)
            if 0 <= i < len(options) and i != correct and i not in partial:
                partial.append(i)
        if partial:
            out["partial"] = sorted(partial)
            out["partial_credit"] = _int(raw.get("partial_credit"), DEFAULT_PARTIAL_CREDIT,
                                         *PARTIAL_CREDIT_RANGE)
        out.update({"code": code, "options": options, "correct": correct,
                    "expected": _text(raw.get("expected"), MAX_EXPECTED),
                    "language": language if language in LANGUAGES else "pseudo"})
    else:  # coding
        lang = language if language in LANGUAGES and language != "pseudo" else "any"
        allowed = clean_languages(raw.get("languages"))
        if lang in CODE_LANGUAGES and allowed and lang not in allowed:
            allowed.insert(0, lang)
        out.update({"code": _text(raw.get("code"), MAX_CODE),  # optional starter code
                    "expected": _text(raw.get("expected"), MAX_EXPECTED),
                    "language": lang})
        if allowed:
            out["languages"] = allowed
        starters = clean_starters(raw.get("starters"), allowed or list(CODE_LANGUAGES))
        if starters:
            out["starters"] = starters
    diff = str(raw.get("difficulty") or "").strip().lower()
    if diff in {"easy", "medium", "hard"}:
        out["difficulty"] = diff
    return out


def clean_languages(raw: Any) -> list[str]:
    """A coding question's allowed languages — known keys only, in order, no repeats.
    Empty means the candidate may use any supported language."""
    out: list[str] = []
    for v in raw if isinstance(raw, (list, tuple)) else []:
        k = str(v or "").strip().lower()
        if k in CODE_LANGUAGES and k not in out:
            out.append(k)
    return out


def clean_starters(raw: Any, allowed: Iterable[str]) -> dict[str, str]:
    """Starter code per language (optional) — only for allowed languages, non-empty."""
    keep = set(allowed)
    out: dict[str, str] = {}
    if isinstance(raw, dict):
        for k, v in list(raw.items())[:MAX_STARTERS * 2]:
            key = str(k or "").strip().lower()
            code = _text(v, MAX_CODE)
            if key in keep and code:
                out[key] = code
    return out


def clean_config(raw: Any) -> dict:
    """The ``weights.skillBank`` block as it will be stored. Raises SkillBankError
    when an ENABLED bank cannot run (no question, a malformed one)."""
    src = raw if isinstance(raw, dict) else {}
    cfg: dict[str, Any] = {"enabled": bool(src.get("enabled")),
                           "ai_fill": bool(src.get("ai_fill", DEFAULTS["ai_fill"]))}
    for key, (lo, hi) in _LIMITS.items():
        cfg[key] = _int(src.get(key), int(DEFAULTS[key]), lo, hi)
    if cfg["pick_max"] < cfg["pick_min"]:
        cfg["pick_max"] = cfg["pick_min"]
    questions_in = src.get("questions") if isinstance(src.get("questions"), list) else []
    if len(questions_in) > MAX_QUESTIONS:
        raise SkillBankError(f"A template can hold at most {MAX_QUESTIONS} questions.")
    questions: list[dict] = []
    seen_ids: set[str] = set()
    for pos, q in enumerate(questions_in, start=1):
        clean = clean_question(q, pos)
        while clean["id"] in seen_ids:
            clean["id"] = uuid.uuid4().hex[:12]
        seen_ids.add(clean["id"])
        questions.append(clean)
    cfg["questions"] = questions
    # Which skills are always asked (ticked and locked on the picker). Chosen per
    # skill in the template editor; absent = the template's required skills.
    if isinstance(src.get("mandatory_skills"), list):
        cfg["mandatory_skills"] = list(dict.fromkeys(
            norm_skill(x) for x in src["mandatory_skills"] if norm_skill(x)))[:50]
    if cfg["enabled"] and not questions:
        raise SkillBankError("Add at least one question to the skill-wise question bank, or switch it off.")
    return cfg


def config_of(weights: Any) -> dict | None:
    """The bank of a template, only when it is switched on and runnable."""
    raw = (weights or {}).get(WEIGHTS_KEY) if isinstance(weights, dict) else None
    if not isinstance(raw, dict) or not raw.get("enabled"):
        return None
    try:
        cfg = clean_config(raw)
    except SkillBankError:
        return None
    return cfg if cfg["questions"] else None


def counts_by_skill(cfg: dict) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for q in cfg.get("questions") or []:
        row = out.setdefault(q["skill"], {k: 0 for k in KINDS})
        row[q["kind"]] += 1
    return out


# ---------------------------------------------------------------------------
# Skill pick
# ---------------------------------------------------------------------------

def pick_options(cfg: dict, required_skills: Iterable[Any]) -> dict:
    """What the candidate is offered: every skill with at least one question;
    the mandatory ones among them ticked and locked — the bank's own
    `mandatory_skills` when RMG / GM marked them, else the template's required skills."""
    by_key: dict[str, str] = {}
    for q in cfg.get("questions") or []:
        by_key.setdefault(skill_key(q["skill"]), q["skill"])
    source = cfg["mandatory_skills"] if isinstance(cfg.get("mandatory_skills"), list) else (required_skills or [])
    required_keys = [skill_key(s) for s in source if skill_key(s)]
    mandatory = [by_key[k] for k in dict.fromkeys(required_keys) if k in by_key]
    others = sorted((v for k, v in by_key.items() if v not in mandatory), key=str.lower)
    offered = mandatory + others
    lo = max(1, min(int(cfg.get("pick_min") or 1), len(offered)))
    hi = max(lo, len(mandatory), min(int(cfg.get("pick_max") or len(offered)), len(offered)))
    hi = min(hi, len(offered)) if offered else 0
    return {"offered": offered, "mandatory": mandatory, "min": lo if offered else 0, "max": hi}


def default_pick(options: dict) -> list[str]:
    """Used until the candidate chooses (and by an older page that never asks):
    the mandatory skills, topped up in order to the minimum."""
    picked = list(options.get("mandatory") or [])
    for s in options.get("offered") or []:
        if len(picked) >= int(options.get("min") or 0):
            break
        if s not in picked:
            picked.append(s)
    return picked


def validate_pick(picked: Iterable[Any], options: dict) -> list[str]:
    """The candidate's ticks, cleaned. Raises SkillBankError with the reason."""
    offered = list(options.get("offered") or [])
    by_key = {skill_key(s): s for s in offered}
    chosen: list[str] = []
    for raw in picked or []:
        k = skill_key(raw)
        if not k:
            continue
        if k not in by_key:
            raise SkillBankError(f"'{norm_skill(raw)}' is not one of this interview's skills.")
        if by_key[k] not in chosen:
            chosen.append(by_key[k])
    for m in options.get("mandatory") or []:
        if m not in chosen:
            chosen.insert(0, m)  # locked — always included
    lo, hi = int(options.get("min") or 0), int(options.get("max") or len(offered))
    if len(chosen) < lo:
        raise SkillBankError(f"Select at least {lo} skills.")
    if hi and len(chosen) > hi:
        raise SkillBankError(f"Select at most {hi} skills.")
    return [s for s in offered if s in chosen]  # the offer's order


# ---------------------------------------------------------------------------
# The question plan
# ---------------------------------------------------------------------------

def _rng(seed: str) -> random.Random:
    return random.Random(int.from_bytes(hashlib.sha256(str(seed or "").encode()).digest()[:8], "big"))


def _round_robin(pools: list[list[dict]], count: int) -> list[dict]:
    out: list[dict] = []
    pools = [list(p) for p in pools]
    while len(out) < count and any(pools):
        for p in pools:
            if p and len(out) < count:
                out.append(p.pop(0))
    return out


def missing_normals(cfg: dict, picked: list[str]) -> list[tuple[str, int]]:
    """(skill, how many spoken questions short) for the picked skills. Empty
    when the template switched AI top-up off.

    With ``spoken_count`` set the shortfall is for the WHOLE interview (the
    count minus every spoken question the picked skills hold), spread round
    robin over the picked skills; otherwise it is per skill against
    ``normal_per_skill``."""
    if not cfg.get("ai_fill", DEFAULTS["ai_fill"]):
        return []
    have: dict[str, int] = {}
    for q in cfg.get("questions") or []:
        if q["kind"] == KIND_NORMAL:
            k = skill_key(q["skill"])
            have[k] = have.get(k, 0) + 1
    total = int(cfg.get("spoken_count") or 0)
    if total > 0:
        short = total - sum(have.get(skill_key(s), 0) for s in picked)
        if short <= 0 or not picked:
            return []
        share = {s: 0 for s in picked}
        i = 0
        while short > 0:
            share[picked[i % len(picked)]] += 1
            short -= 1
            i += 1
        return [(s, n) for s, n in share.items() if n > 0]
    want = int(cfg.get("normal_per_skill") or 0)
    return [(s, want - have.get(skill_key(s), 0)) for s in picked if want - have.get(skill_key(s), 0) > 0]


def interleave(normals: list[dict], typed: list[dict]) -> list[dict]:
    """Theory first, typed questions IN BETWEEN (11 Oct 2026): each typed
    question goes after an evenly spaced share of the spoken ones, so the
    interview never opens or closes on a screen-only question while there are
    spoken ones to put around it. No spoken questions → the typed ones alone."""
    n, t = len(normals), len(typed)
    if not t:
        return list(normals)
    if not n:
        return list(typed)
    cuts = []
    for j in range(t):
        pos = int((j + 1) * n / (t + 1) + 0.5)
        pos = max(1, min(pos, n - 1 if n > 1 else 1))
        cuts.append(pos)
    out: list[dict] = []
    ti = 0
    for i, q in enumerate(normals, start=1):
        out.append(q)
        while ti < t and cuts[ti] == i:
            out.append(typed[ti])
            ti += 1
    out.extend(typed[ti:])
    return out


def build_plan(cfg: dict, picked: list[str], seed: str,
               extra_normals: dict[str, list[dict]] | None = None) -> list[dict]:
    """The ordered questions for the picked skills.

    Per picked skill up to ``normal_per_skill`` normal questions (the bank's
    first, then any AI top-up in ``extra_normals`` keyed by ``skill_key``);
    across the picked skills (mandatory first, round robin) ``pseudo_count``
    pseudo-code and ``coding_count`` coding questions. The typed questions sit
    between the spoken ones (``interleave``), the coding problem in the middle
    of them so a candidate who runs short of time loses a spoken question, not
    the coding."""
    rng = _rng(seed)
    keys = [skill_key(s) for s in picked]
    by_kind_skill: dict[tuple[str, str], list[dict]] = {}
    for q in cfg.get("questions") or []:
        by_kind_skill.setdefault((q["kind"], skill_key(q["skill"])), []).append(q)
    for pool in by_kind_skill.values():
        rng.shuffle(pool)

    extra = extra_normals or {}
    spoken_total = int(cfg.get("spoken_count") or 0)
    if spoken_total > 0:
        # The template says how many spoken questions in all: a random draw
        # (seeded per interview) across the picked skills, round robin so every
        # skill is covered before any repeats.
        normal_pools = [by_kind_skill.get((KIND_NORMAL, k), []) + list(extra.get(k) or []) for k in keys]
        normals = _round_robin(normal_pools, spoken_total)
    else:
        per_skill = int(cfg["normal_per_skill"])
        normal_pools = [(by_kind_skill.get((KIND_NORMAL, k), []) + list(extra.get(k) or []))[:per_skill]
                        for k in keys]
        normals = _round_robin(normal_pools, sum(len(p) for p in normal_pools))
    pseudo = _round_robin([by_kind_skill.get((KIND_PSEUDO, k), []) for k in keys], int(cfg["pseudo_count"]))
    coding = _round_robin([by_kind_skill.get((KIND_CODING, k), []) for k in keys], int(cfg["coding_count"]))
    lead = (len(pseudo) + 1) // 2
    typed = pseudo[:lead] + coding + pseudo[lead:]
    return [to_spec(q, cfg) for q in interleave(normals, typed)]


def plan_summary(cfg: dict, required_skills: Iterable[Any] = ()) -> dict:
    """What one interview from this bank holds — for the candidate's rules page
    and the template list (12 Oct 2026: the rules page used to print the
    template's old "question count" instead of the questions really asked).

    Counted on the default pick (the always-asked skills, topped up to the
    minimum); ``varies`` is True when the candidate picks skills, because the
    spoken / typed mix then follows the pick. AI top-up questions count."""
    options = pick_options(cfg, required_skills)
    picked = default_pick(options)
    specs = build_plan(cfg, picked, "summary")
    counts = {k: 0 for k in KINDS}
    for sp in specs:
        counts[sp.get("kind") or KIND_NORMAL] += 1
    counts[KIND_NORMAL] += sum(n for _s, n in missing_normals(cfg, picked))
    offered, mandatory = options.get("offered") or [], options.get("mandatory") or []
    varies = any(s not in mandatory for s in offered)
    minutes = (counts[KIND_NORMAL] * SPOKEN_MINUTES_EACH
               + counts[KIND_PSEUDO] * int(cfg.get("pseudo_minutes") or DEFAULTS["pseudo_minutes"])
               + counts[KIND_CODING] * int(cfg.get("coding_minutes") or DEFAULTS["coding_minutes"]))
    return {
        "total": sum(counts.values()),
        "spoken": counts[KIND_NORMAL],
        "pseudo": counts[KIND_PSEUDO],
        "coding": counts[KIND_CODING],
        "varies": varies,
        "pick_min": int(options.get("min") or 0),
        "pick_max": int(options.get("max") or 0),
        "skills": list(offered),
        "mandatory": list(mandatory),
        "minutes_estimate": int(round(minutes + 1)),
        "coding_minutes": int(cfg.get("coding_minutes") or DEFAULTS["coding_minutes"]),
        "pseudo_minutes": int(cfg.get("pseudo_minutes") or DEFAULTS["pseudo_minutes"]),
    }


def to_spec(q: dict, cfg: dict) -> dict:
    """The per-question details kept on the session (never sent whole to the
    candidate — see ``public_spec``)."""
    spec: dict[str, Any] = {"qid": q["id"], "skill": q["skill"], "kind": q["kind"], "text": q["text"]}
    for key in ("code", "language", "languages", "starters", "options", "correct", "partial",
                "partial_credit", "expected", "difficulty", "source"):
        if key in q:
            spec[key] = q[key]
    if q["kind"] == KIND_CODING:
        spec["time_limit_sec"] = int(cfg["coding_minutes"]) * 60
    elif q["kind"] == KIND_PSEUDO:
        spec["time_limit_sec"] = int(cfg["pseudo_minutes"]) * 60
    return spec


def session_questions(specs: list[dict]) -> list[str]:
    """The question strings for ``session["questions"]``. Two pseudo-code
    questions often share wording ("What does this print?"), so repeats get a
    numbered suffix — the strings must stay distinct for the uniqueness guards."""
    seen: dict[str, int] = {}
    out: list[str] = []
    for spec in specs:
        text = " ".join(str(spec.get("text") or "").split())
        n = seen.get(text.lower(), 0) + 1
        seen[text.lower()] = n
        out.append(text if n == 1 else f"{text} ({n})")
    return out


def stamp_specs(meta: dict, specs: list[dict], offset: int) -> None:
    meta["question_specs"] = {str(offset + i): spec for i, spec in enumerate(specs)}


def is_skill_bank(meta: Any) -> bool:
    return str((meta or {}).get("question_source") or "") == QUESTION_SOURCE


def spec_for(meta: Any, idx: int) -> dict | None:
    specs = (meta or {}).get("question_specs") if isinstance(meta, dict) else None
    if not isinstance(specs, dict):
        return None
    spec = specs.get(str(int(idx)))
    return spec if isinstance(spec, dict) else None


def kind_for(meta: Any, idx: int) -> str:
    spec = spec_for(meta, idx)
    return str((spec or {}).get("kind") or KIND_NORMAL)


def is_typed(meta: Any, idx: int) -> bool:
    return kind_for(meta, idx) in TYPED_KINDS


def shift_after_insert(meta: dict, idx: int) -> None:
    """A conversation follow-up was inserted at ``idx`` — every spec at or after
    it moves one place down (the follow-up itself is a normal spoken question)."""
    specs = meta.get("question_specs") if isinstance(meta, dict) else None
    if not isinstance(specs, dict) or not specs:
        return
    moved: dict[str, Any] = {}
    for key, spec in specs.items():
        try:
            i = int(key)
        except (TypeError, ValueError):
            continue
        moved[str(i + 1 if i >= idx else i)] = spec
    meta["question_specs"] = moved


def public_spec(spec: dict | None) -> dict:
    """The fields the candidate's page needs — never the correct option, never
    the expected answer."""
    if not spec:
        return {"kind": KIND_NORMAL}
    out: dict[str, Any] = {"kind": spec.get("kind") or KIND_NORMAL, "skill": spec.get("skill") or ""}
    if out["kind"] in TYPED_KINDS:
        out["code"] = spec.get("code") or ""
        out["language"] = spec.get("language") or ""
        out["answer_time_limit_sec"] = int(spec.get("time_limit_sec") or 0)
        out["speak"] = False
    if out["kind"] == KIND_PSEUDO:
        out["options"] = list(spec.get("options") or [])
    if out["kind"] == KIND_CODING:
        out["languages"] = list(spec.get("languages") or CODE_LANGUAGES)
        out["starters"] = dict(spec.get("starters") or {})
    return out


def question_map(meta: Any, total_questions: int) -> list[dict]:
    """The candidate's progress strip (12 Oct 2026): kind + skill of every scored
    question in order (warm-ups left out, so position i is "Question i+1").
    Never the text, options or answers — only what the strip draws."""
    if not is_skill_bank(meta):
        return []
    warm = {int(v) for v in ((meta or {}).get("warmup_indices") or []) if str(v).lstrip("-").isdigit()}
    out = []
    for i in range(max(0, int(total_questions))):
        if i in warm:
            continue
        spec = spec_for(meta, i) or {}
        out.append({"kind": str(spec.get("kind") or KIND_NORMAL), "skill": str(spec.get("skill") or "")})
    return out


def chosen_language(spec: dict | None, raw: Any) -> str | None:
    """The language the candidate wrote a coding answer in — one the question
    allows, else None (the question's own language is used)."""
    if not spec or spec.get("kind") != KIND_CODING:
        return None
    lang = str(raw or "").strip().lower()
    allowed = list(spec.get("languages") or CODE_LANGUAGES)
    return lang if lang in allowed else None


def record_language(meta: dict, idx: int, lang: str | None) -> None:
    """Keep the candidate's coding language on the session's spec, where the
    scoring prompt and the report read it."""
    spec = spec_for(meta, idx)
    if spec is not None and lang:
        spec["answered_language"] = lang


def answer_language(spec: dict | None) -> str:
    if not spec:
        return ""
    return str(spec.get("answered_language") or spec.get("language") or "")


# ---------------------------------------------------------------------------
# Answers
# ---------------------------------------------------------------------------

OPTION_LETTERS = "ABCDEF"


def choice_answer(spec: dict, choice: Any) -> tuple[int, str]:
    """(choice index, the answer text stored for the turn). Raises SkillBankError
    for an index outside the options."""
    options = list(spec.get("options") or [])
    try:
        i = int(str(choice).strip())
    except (TypeError, ValueError):
        raise SkillBankError("Select one of the options.")
    if not 0 <= i < len(options):
        raise SkillBankError("Select one of the options.")
    return i, f"Option {OPTION_LETTERS[i]}: {options[i]}"


def choice_credit(spec: dict, choice: Any) -> float:
    """Share of the marks an option earns: 1.0 for the correct one, the
    question's ``partial_credit`` % for a "partly right" one, else 0."""
    try:
        i = int(choice)
    except (TypeError, ValueError):
        return 0.0
    if i == int(spec.get("correct", -1)):
        return 1.0
    if i in [int(x) for x in (spec.get("partial") or [])]:
        return max(0.0, min(1.0, int(spec.get("partial_credit") or DEFAULT_PARTIAL_CREDIT) / 100.0))
    return 0.0


def record_choice(meta: dict, idx: int, choice: int) -> bool:
    """Keep the chosen option beside the answer; returns whether it is correct."""
    spec = spec_for(meta, idx) or {}
    credit = choice_credit(spec, choice)
    picks = meta.setdefault("choices", {})
    picks[str(int(idx))] = {"choice": int(choice), "correct": credit >= 1.0, "credit": credit}
    return credit >= 1.0


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def specs_for_evaluation(meta: Any, total_questions: int, n_answered: int) -> list[dict | None]:
    """Specs aligned to the evaluation's question list: warm-ups dropped, then
    the answered prefix (mirrors ``filter_out_warmups`` + ``align_qa_to_answered_turns``)."""
    if not isinstance(meta, dict) or not isinstance(meta.get("question_specs"), dict):
        return [None] * max(0, n_answered)
    warm = set()
    for v in meta.get("warmup_indices") or []:
        try:
            warm.add(int(v))
        except (TypeError, ValueError):
            continue
    aligned = [spec_for(meta, i) for i in range(total_questions) if i not in warm]
    out = aligned[:n_answered]
    while len(out) < n_answered:
        out.append(None)
    return out


def choices_for_evaluation(meta: Any, total_questions: int, n_answered: int) -> list[dict | None]:
    picks = (meta or {}).get("choices") if isinstance(meta, dict) else None
    picks = picks if isinstance(picks, dict) else {}
    warm = {int(v) for v in ((meta or {}).get("warmup_indices") or []) if str(v).lstrip("-").isdigit()}
    aligned = [picks.get(str(i)) for i in range(total_questions) if i not in warm]
    out = aligned[:n_answered]
    while len(out) < n_answered:
        out.append(None)
    return out


def pseudo_row(question_index: int, spec: dict, answer: str, pick: dict | None) -> dict:
    """The full per-question row for a pseudo-code answer — no model call."""
    options = list(spec.get("options") or [])
    right = int(spec.get("correct", -1))
    chosen = int(pick["choice"]) if isinstance(pick, dict) and pick.get("choice") is not None else _choice_from_answer(answer)
    credit = choice_credit(spec, chosen) if chosen is not None else 0.0
    ok = credit >= 1.0
    partly = 0.0 < credit < 1.0
    right_text = f"Option {OPTION_LETTERS[right]}: {options[right]}" if 0 <= right < len(options) else ""
    chose_text = f"Option {OPTION_LETTERS[chosen]}: {options[chosen]}" if chosen is not None and 0 <= chosen < len(options) else "No option selected"
    explanation = str(spec.get("expected") or "").strip()
    score = round(10.0 * credit, 1)
    if ok:
        summary = f"Correct — selected {chose_text}."
    elif partly:
        summary = (f"Partly right — selected {chose_text} ({int(credit * 100)}% credit); "
                   f"the fully correct answer is {right_text}.")
    else:
        summary = f"Incorrect — selected {chose_text}; the correct answer is {right_text}."
    return {
        "question_index": question_index,
        "score": score,
        "overall_rating": score,
        "summary": summary,
        "feedback": summary,
        "interview_feedback": summary + (f" {explanation}" if explanation else ""),
        "expected_answer": right_text + (f" — {explanation}" if explanation else ""),
        "ideal_answer": right_text,
        "correct_concepts": ([{"topic": spec.get("skill") or "Pseudo-code", "explanation": "Read the code correctly."}]
                             if ok else [{"topic": spec.get("skill") or "Pseudo-code",
                                          "explanation": "Read the code partly right."}] if partly else []),
        "improvement_areas": [] if ok else [{"topic": spec.get("skill") or "Pseudo-code",
                                             "explanation": "Partly misread the pseudo-code." if partly
                                             else "Misread the pseudo-code.",
                                             "correction": right_text}],
        "strengths": (["Read the pseudo-code correctly."] if ok else ["Read part of the pseudo-code correctly."]
                      if partly else ["No significant technical strengths identified."]),
        "weaknesses": [] if ok else ["Selected a partly right option."] if partly else ["Selected the wrong option."],
        "credit": credit,
        "follow_up_questions": ["Walk me through how you traced this code."],
        "dimension_scores": {"technical_accuracy": int(score * 10), "concept_coverage": int(score * 10),
                             "depth": int(score * 10), "communication": 0, "confidence": int(score * 10)},
        "evidence": chose_text,
        "evaluation_reason": "pseudo_choice",
        "auto_marked": True,
    }


def _choice_from_answer(answer: str) -> int | None:
    m = re.match(r"\s*option\s+([A-F])\b", str(answer or ""), re.I)
    return OPTION_LETTERS.index(m.group(1).upper()) if m else None


def decorate_row(row: dict, spec: dict | None, pick: dict | None = None) -> dict:
    """Stamp the kind / skill (and the shown code / options) on a report row so
    the report page can render it the way the candidate saw it."""
    if not isinstance(row, dict) or not spec:
        return row
    row["question_kind"] = spec.get("kind") or KIND_NORMAL
    row["skill"] = spec.get("skill") or row.get("skill") or ""
    if spec.get("kind") in TYPED_KINDS:
        lang = answer_language(spec)
        row["code"] = (spec.get("starters") or {}).get(lang) or spec.get("code") or ""
        row["language"] = lang
    if spec.get("kind") == KIND_PSEUDO:
        row["options"] = list(spec.get("options") or [])
        row["correct_option"] = int(spec.get("correct", -1))
        if spec.get("partial"):
            row["partial_options"] = [int(x) for x in spec["partial"]]
            row["partial_credit"] = int(spec.get("partial_credit") or DEFAULT_PARTIAL_CREDIT)
        if isinstance(pick, dict) and pick.get("choice") is not None:
            row["selected_option"] = int(pick["choice"])
    if spec.get("kind") == KIND_CODING:
        row["verdict"] = coding_verdict(row)
    if spec.get("source") == "ai":
        row["question_source"] = "ai_fill"
    return row


VERDICTS = ("correct", "partially_correct", "incorrect")
#: Score bands (0-10) a coding verdict falls back to when the model gave none.
VERDICT_CORRECT_AT = 7.0
VERDICT_PARTIAL_AT = 4.0


def coding_verdict(row: dict) -> str:
    """The coding answer in one word for the report (11 Oct 2026: "the AI checks
    whether it is right or wrong"). The model's own verdict when it gave a valid
    one, else read off the score; a skipped / blank answer is always incorrect."""
    raw = str((row or {}).get("verdict") or "").strip().lower().replace(" ", "_").replace("-", "_")
    if raw in {"partial", "partly_correct"}:
        raw = "partially_correct"
    if raw == "wrong":
        raw = "incorrect"
    if raw in VERDICTS:
        return raw
    try:
        score = float((row or {}).get("score") or 0.0)
    except (TypeError, ValueError):
        score = 0.0
    if score >= VERDICT_CORRECT_AT:
        return "correct"
    if score >= VERDICT_PARTIAL_AT:
        return "partially_correct"
    return "incorrect"


def evaluation_extra(spec: dict | None) -> dict | None:
    """What the scoring prompt is told about a normal / coding question."""
    if not spec or spec.get("kind") == KIND_PSEUDO:
        return None
    extra: dict[str, Any] = {"kind": "typed_code" if spec.get("kind") == KIND_CODING else "spoken"}
    if spec.get("expected"):
        extra["reference_answer"] = str(spec["expected"])[:MAX_EXPECTED]
    if spec.get("kind") == KIND_CODING:
        lang = answer_language(spec) or "any"
        extra["language"] = LANGUAGE_LABELS.get(lang, lang)
        starter = (spec.get("starters") or {}).get(lang) or spec.get("code")
        if starter:
            extra["starter_code"] = str(starter)[:2000]
    return extra


def _valid_at(valid: list[bool] | None, i: int) -> bool:
    return True if valid is None else (i < len(valid) and bool(valid[i]))


def skill_scores_from_rows(rows: list[dict], specs: list[dict | None], picked: list[str],
                           valid: list[bool] | None = None) -> list[dict]:
    """Per picked skill: the mean of its scored questions (0-10), with the
    evidence of its best answer. Skipped / unanswered turns are left out, the
    same rule as the overall score; a skill with nothing answered scores 0."""
    buckets: dict[str, list[dict]] = {}
    for i, (row, spec) in enumerate(zip(rows or [], specs or [])):
        if not spec or not isinstance(row, dict) or not _valid_at(valid, i):
            continue
        buckets.setdefault(skill_key(spec.get("skill")), []).append(row)
    out = []
    for skill in picked or []:
        items = buckets.get(skill_key(skill), [])
        scores = []
        for r in items:
            try:
                scores.append(float(r.get("score") or 0.0))
            except (TypeError, ValueError):
                scores.append(0.0)
        mean = round(sum(scores) / len(scores), 1) if scores else 0.0
        best = max(items, key=lambda r: float(r.get("score") or 0.0), default=None)
        out.append({
            "skill": skill,
            "score": mean,
            "questions": len(items),
            "evidence": str((best or {}).get("evidence") or "")[:300],
        })
    return out


def kind_summary(rows: list[dict], specs: list[dict | None], valid: list[bool] | None = None) -> dict:
    """Score per question kind for the report header (normal / pseudo / coding)."""
    out: dict[str, dict] = {}
    for i, (row, spec) in enumerate(zip(rows or [], specs or [])):
        if not spec or not isinstance(row, dict) or not _valid_at(valid, i):
            continue
        k = spec.get("kind") or KIND_NORMAL
        cell = out.setdefault(k, {"questions": 0, "total": 0.0})
        cell["questions"] += 1
        try:
            cell["total"] += float(row.get("score") or 0.0)
        except (TypeError, ValueError):
            pass
    for cell in out.values():
        cell["mean"] = round(cell["total"] / cell["questions"], 1) if cell["questions"] else 0.0
        cell.pop("total", None)
    if KIND_PSEUDO in out:
        correct = sum(1 for i, (row, spec) in enumerate(zip(rows or [], specs or []))
                      if spec and spec.get("kind") == KIND_PSEUDO and _valid_at(valid, i)
                      and float((row or {}).get("score") or 0) >= 10)
        out[KIND_PSEUDO]["correct"] = correct
        out[KIND_PSEUDO]["partial"] = sum(
            1 for i, (row, spec) in enumerate(zip(rows or [], specs or []))
            if spec and spec.get("kind") == KIND_PSEUDO and _valid_at(valid, i)
            and 0 < float((row or {}).get("score") or 0) < 10)
    if KIND_CODING in out:
        out[KIND_CODING]["verdicts"] = {v: sum(
            1 for i, (row, spec) in enumerate(zip(rows or [], specs or []))
            if spec and spec.get("kind") == KIND_CODING and _valid_at(valid, i)
            and coding_verdict(row or {}) == v) for v in VERDICTS}
    return out
