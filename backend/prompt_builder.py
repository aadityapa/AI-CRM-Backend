"""
Production-grade prompt builder for interview question generation.

Constructs structured System + User prompts with:
- Experience-aware questioning tiers
- Strict skill mapping enforcement
- Anti-generic / anti-textbook rules
- Difficulty progression per question slot
- Post-generation validation layer
"""

from __future__ import annotations

import re
from typing import List


# ──────────────────────────────────────────────────────────
#  Experience tiers — drives question depth & framing
# ──────────────────────────────────────────────────────────

_EXPERIENCE_TIERS = {
    "junior": {
        "range": "0-1 years",
        "label": "Junior / Entry-level",
        "focus": (
            "fundamentals, simple implementations, basic debugging, "
            "code-reading ability, tool usage basics"
        ),
        "question_style": (
            "Ask about hands-on basics: writing a small feature, fixing a simple bug, "
            "explaining what a code snippet does, basic tool/library usage. "
            "Expect the candidate to explain their thought process, not recite definitions."
        ),
        "avoid": (
            "Do NOT ask about system design, distributed systems, scaling strategies, "
            "or architectural trade-offs. Do NOT expect production war stories."
        ),
    },
    "mid": {
        "range": "2-4 years",
        "label": "Mid-level",
        "focus": (
            "debugging real issues, API design, performance profiling, "
            "testing strategies, code review, integration patterns"
        ),
        "question_style": (
            "Ask about debugging production bugs, designing APIs, optimizing queries, "
            "writing meaningful tests, handling edge cases, choosing between approaches. "
            "Expect concrete examples from their work."
        ),
        "avoid": (
            "Do NOT ask textbook definitions or entry-level syntax questions. "
            "Do NOT ask about large-scale distributed architecture unless relevant to the role."
        ),
    },
    "senior": {
        "range": "5+ years",
        "label": "Senior / Staff",
        "focus": (
            "architecture decisions, scaling strategies, production incident handling, "
            "trade-off analysis, system design, mentoring decisions, technical debt management"
        ),
        "question_style": (
            "Ask about architectural trade-offs, production incidents they resolved, "
            "scaling decisions, how they'd design or refactor a system, "
            "cross-team technical decisions, and lessons from failures. "
            "Expect depth, nuance, and evidence of ownership."
        ),
        "avoid": (
            "Do NOT ask basic implementation or syntax questions. "
            "Do NOT ask anything a 1-year developer could answer equally well."
        ),
    },
}


def _resolve_experience_tier(experience: str) -> dict:
    """Map raw experience string to a tier config."""
    exp = (experience or "").strip().lower()
    if not exp or exp in ("not specified", "n/a", "unknown", "-", "0"):
        return _EXPERIENCE_TIERS["mid"]

    numbers = re.findall(r"(\d+)", exp)
    if numbers:
        years = max(int(n) for n in numbers)
        if years <= 1:
            return _EXPERIENCE_TIERS["junior"]
        if years <= 4:
            return _EXPERIENCE_TIERS["mid"]
        return _EXPERIENCE_TIERS["senior"]

    for keyword in ("fresher", "intern", "junior", "entry", "graduate", "beginner"):
        if keyword in exp:
            return _EXPERIENCE_TIERS["junior"]
    for keyword in ("senior", "staff", "lead", "principal", "architect", "manager"):
        if keyword in exp:
            return _EXPERIENCE_TIERS["senior"]

    return _EXPERIENCE_TIERS["mid"]


# ──────────────────────────────────────────────────────────
#  Difficulty slot assignment
# ──────────────────────────────────────────────────────────

_DIFFICULTY_SLOTS = {
    "easy": {
        1: "warmup",
        2: "warmup",
        3: "easy-practical",
        4: "medium-practical",
        5: "medium-practical",
    },
    "medium": {
        1: "warmup",
        2: "practical-implementation",
        3: "debugging-problem-solving",
        4: "optimization-scaling",
        5: "advanced-real-world",
    },
    "hard": {
        1: "practical-implementation",
        2: "debugging-problem-solving",
        3: "optimization-scaling",
        4: "advanced-architecture",
        5: "production-incident-scenario",
    },
}

_SLOT_DESCRIPTIONS = {
    "warmup": "Easy warmup — a quick, concrete question to get the candidate comfortable. Practical, not theoretical.",
    "easy-practical": "Simple practical task — ask about a basic implementation, a small feature, or reading/explaining code.",
    "practical-implementation": "Implementation-focused — ask about building something, coding a solution, or designing a small component.",
    "debugging-problem-solving": "Debugging / problem-solving — present a bug scenario, error message, or broken behavior to diagnose.",
    "optimization-scaling": "Optimization / scaling — ask about improving performance, reducing latency, or handling more load.",
    "medium-practical": "Practical mid-level — a hands-on question mixing implementation with some real-world context.",
    "advanced-real-world": "Advanced real-world — a scenario from production: incident response, architectural decision, or trade-off analysis.",
    "advanced-architecture": "Architecture — ask about system design decisions, service boundaries, or infrastructure trade-offs.",
    "production-incident-scenario": "Production incident — describe a realistic production failure and ask how they'd investigate and resolve it.",
}


def _build_slot_instructions(n: int, difficulty: str) -> str:
    """Build per-question slot instructions for difficulty progression."""
    diff = (difficulty or "medium").strip().lower()
    if diff not in _DIFFICULTY_SLOTS:
        diff = "medium"
    template = _DIFFICULTY_SLOTS[diff]
    lines = []
    for i in range(1, n + 1):
        if i in template:
            slot = template[i]
        elif i <= 2:
            slot = template.get(1, "warmup")
        elif i <= n * 0.6:
            slot = template.get(3, "debugging-problem-solving")
        else:
            slot = template.get(5, "advanced-real-world")
        desc = _SLOT_DESCRIPTIONS.get(slot, slot)
        lines.append(f"  Q{i}: [{slot.upper()}] {desc}")
    return "\n".join(lines)


# ──────────────────────────────────────────────────────────
#  SYSTEM PROMPT builder
# ──────────────────────────────────────────────────────────

_SENIORITY_DEPTH = {
    "junior": "ask hands-on basics — small features, simple bugs, code reading, basic tool usage.",
    "mid": "ask about debugging real bugs, API design, query optimization, testing strategies, choosing between approaches.",
    "senior": "ask about architecture trade-offs, scaling decisions, production incidents, technical debt, cross-team ownership.",
}


def _extract_seniority_label(experience: str) -> str:
    """Return the seniority label portion from an experience string."""
    exp = (experience or "").strip()
    if not exp:
        return ""
    head = exp.split(";", 1)[0].strip()
    return head


def _seniority_depth_hint(experience: str) -> str:
    """Return a one-line depth hint matched to the candidate's seniority."""
    tier = _resolve_experience_tier(experience)
    if tier is _EXPERIENCE_TIERS["junior"]:
        return _SENIORITY_DEPTH["junior"]
    if tier is _EXPERIENCE_TIERS["senior"]:
        return _SENIORITY_DEPTH["senior"]
    return _SENIORITY_DEPTH["mid"]


# May 2026: plain, conversational interviewer tone (reduces candidate confusion).
_QUESTION_LANGUAGE_RULES = (
    " Write every question in simple, professional, easy-to-understand English."
    " Sound like a real human interviewer: concise, practical, conversational."
    " Avoid robotic phrasing, unnecessary jargon, and long compound sentences."
    " Prefer short questions (roughly 8–22 words) unless a scenario truly needs more."
)

_SYSTEM_PROMPT_BATCH = (
    "You are an interview question generator for technical hiring."
    " Produce only interview questions. Output exactly the requested count, one question per line,"
    " no numbering, no bullets, no markdown, no preamble, no commentary."
    " Each line must end with '?'. Do not merge multiple questions into one line."
    + _QUESTION_LANGUAGE_RULES
)


def build_system_prompt(
    skills: List[str],
    experience: str = "",
    difficulty: str = "medium",
    domain_categories: List[tuple[str, str]] | None = None,
) -> str:
    """Static system prompt used as a stable prefix (eligible for provider-side cache).

    Keeping it stable across calls means cached prefix tokens are reused on supported
    APIs, reducing per-call prompt token cost.
    """
    return _SYSTEM_PROMPT_BATCH


def _domain_names_only(domain_categories: List | None) -> List[str]:
    """Normalize domain payloads to heading/title strings only (no descriptions)."""
    names: List[str] = []
    for item in domain_categories or []:
        if isinstance(item, (list, tuple)) and item:
            name = str(item[0] or "").strip()
        else:
            name = str(item or "").strip()
        if name and name not in names:
            names.append(name)
    return names


def _trim_to_sentence(text: str, limit: int) -> str:
    """Trim text to limit chars on a sentence boundary when possible."""
    if not text:
        return ""
    s = " ".join(text.split())
    if len(s) <= limit:
        return s
    cut = s[:limit]
    last_period = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "))
    if last_period >= int(limit * 0.6):
        return cut[: last_period + 1]
    last_space = cut.rfind(" ")
    return (cut[:last_space] if last_space > 0 else cut).rstrip(",;:- ") + "…"


# ──────────────────────────────────────────────────────────
#  USER PROMPT builder — batch question generation
# ──────────────────────────────────────────────────────────

def build_user_prompt_batch(
    n: int,
    skills: List[str],
    difficulty: str = "medium",
    experience: str = "",
    jd_text: str = "",
    cv_text: str = "",
    coach_hints: str = "",
    avoid_history: List[str] | None = None,
    jd_char_limit: int = 2200,
    cv_char_limit: int = 1500,
    domain_categories: List[tuple[str, str]] | None = None,
    variety_seed: str = "",
) -> str:
    """Lean user prompt — skill is the topic, domain is the type, seniority is the depth.

    variety_seed: an opaque per-request nonce that nudges the model to produce
    different wording on each call, even with identical inputs.
    """
    skills_list = [s.strip() for s in skills[:12] if s.strip()]
    skills_str = ", ".join(skills_list) if skills_list else "core technical skills"

    seniority_label = _extract_seniority_label(experience) or "Mid"
    depth_hint = _seniority_depth_hint(experience)

    domain_names = _domain_names_only(domain_categories)
    domain_block = ""
    if domain_names:
        domain_lines = "\n".join(f"- {name}" for name in domain_names)
        domain_block = f"\nASSESSMENT DOMAINS (use these to shape question type):\n{domain_lines}\n"

    assignment_block = ""
    if domain_names:
        assignment_lines: List[str] = []
        for i in range(1, n + 1):
            domain = domain_names[(i - 1) % len(domain_names)]
            skill = skills_list[(i - 1) % len(skills_list)] if skills_list else "any"
            assignment_lines.append(f"Q{i}: skill={skill} | domain={domain}")
        assignment_block = "\nPER-QUESTION ASSIGNMENT:\n" + "\n".join(assignment_lines) + "\n"

    jd_block = ""
    jd = (jd_text or "").strip()
    if jd:
        jd_block = f"\nJD: {_trim_to_sentence(jd, jd_char_limit)}\n"

    cv_block = ""
    cv = (cv_text or "").strip()
    if cv:
        cv_block = f"\nCV: {_trim_to_sentence(cv, cv_char_limit)}\n"

    history_block = ""
    ah = [x.strip() for x in (avoid_history or []) if x and str(x).strip()]
    if ah:
        shown = ah[-30:]
        history_block = (
            "\nDO NOT repeat or paraphrase any of these previously generated questions "
            "(produce a fresh batch with different angles, scenarios, and wording):\n- "
            + "\n- ".join(str(q)[:200] for q in shown)
            + "\n"
        )

    seed_block = ""
    if variety_seed:
        seed_block = (
            f"\nVARIETY TOKEN: {variety_seed} (this is a freshness nonce — use it to "
            "produce different scenarios than any previous request; do NOT echo the token)\n"
        )

    strict_lines = ""
    if domain_names:
        strict_lines = (
            f"\nLine i must match Q{{i}} in PER-QUESTION ASSIGNMENT (use that row's domain and skill).\n"
        )
    coach_block = ""
    if (coach_hints or "").strip():
        coach_block = (
            "\nTEMPLATE AI PROMPT INSTRUCTIONS (authoritative behavior guidance):\n"
            + coach_hints.strip()[:4200]
            + "\n"
        )

    slot_plan = _build_slot_instructions(n, difficulty)
    style_rotation = (
        "\nQUESTION STYLE ROTATION (must mix these naturally across the set):\n"
        "- Implementation (how to build)\n"
        "- Debugging (what to check first)\n"
        "- Failure/Incident (production outage, flaky tests, CI failures)\n"
        "- Architecture (design/scale/reliability)\n"
        "- Optimization (latency, throughput, execution time)\n"
        "- Trade-off (speed vs quality, cost vs reliability)\n"
        "- Scenario-based (deadline pressure, changing requirements)\n"
        "- Leadership (mentoring, quality standards, cross-team ownership)\n"
    )

    return (
        f"Generate {n} interview questions.\n"
        f"Use simple English a candidate can understand on first listen.\n"
        f"Vary wording and scenarios — do not repeat opening patterns from PREVIOUSLY ASKED.\n"
        f"Do not start more than one question with the same first 3 words.\n"
        f"SENIORITY: {seniority_label} - {depth_hint}\n"
        f"SKILLS: {skills_str}\n"
        f"{style_rotation}\nDIFFICULTY ESCALATION PLAN:\n{slot_plan}\n"
        f"{coach_block}{domain_block}{assignment_block}{jd_block}{cv_block}{history_block}{seed_block}{strict_lines}"
    )


# ──────────────────────────────────────────────────────────
#  USER PROMPT builder — one question per skill
# ──────────────────────────────────────────────────────────

def build_user_prompt_per_skill(
    skills: List[str],
    difficulty: str = "medium",
    experience: str = "",
    jd_text: str = "",
    cv_text: str = "",
    coach_hints: str = "",
    avoid_history: List[str] | None = None,
) -> str:
    """Build the user-level prompt for one-question-per-skill generation."""
    tier = _resolve_experience_tier(experience)
    skills_str = ", ".join(s.strip() for s in skills[:12] if s.strip())

    coach_block = ""
    if (coach_hints or "").strip():
        coach_block = f"\nDEPLOYMENT MEMORY:\n{coach_hints.strip()[:800]}\n"

    history_block = ""
    ah = [h.strip() for h in (avoid_history or []) if h and h.strip()]
    if ah:
        history_block = (
            "\nPREVIOUSLY ASKED (generate completely different questions):\n"
            + "\n".join(f"- {q[:200]}" for q in ah[-40:])
            + "\n"
        )

    exp_display = experience if experience and experience.strip() not in ("", "Not specified") else "Not specified"

    return f"""Generate exactly {len(skills)} questions — one per skill, in this exact order:
{skills_str}

CANDIDATE: {exp_display} ({tier['label']})
DIFFICULTY: {difficulty}

JD CONTEXT: {(jd_text or '')[:3600]}
CV CONTEXT: {(cv_text or '')[:2200]}
{coach_block}{history_block}
RULES:
- Question 1 targets skill 1, Question 2 targets skill 2, etc.
- Each question must be a scenario, debugging case, or implementation task — NOT a definition.
- Under 170 characters per question.
- One line per question, no numbering, no bullets.
- No repeated sentence structures.

Return ONLY the {len(skills)} questions."""


# ──────────────────────────────────────────────────────────
#  USER PROMPT builder — adaptive follow-up
# ──────────────────────────────────────────────────────────

def build_user_prompt_followup(
    skills: List[str],
    previous_question: str,
    previous_answer: str,
    jd_text: str = "",
    recent_transcript: str = "",
    avoid_questions: List[str] | None = None,
    coach_hints: str = "",
) -> str:
    """Build the user-level prompt for adaptive follow-up generation."""
    skills_str = ", ".join(s.strip() for s in skills[:12] if s.strip()) or "core technical skills"
    avoid = avoid_questions or []
    avoid_text = "\n".join(f"- {q[:220]}" for q in avoid[-8:]) if avoid else "(none)"
    transcript = (recent_transcript or "").strip()[:2600]

    coach_block = ""
    if (coach_hints or "").strip():
        coach_block = f"\nDEPLOYMENT MEMORY:\n{coach_hints.strip()[:650]}\n"

    conversation_block = ""
    if transcript:
        conversation_block = f"RECENT CONVERSATION:\n{transcript}"
    else:
        conversation_block = f"LAST EXCHANGE:\nQ: {previous_question[:450]}\nA: {previous_answer[:1000]}"

    return f"""Generate exactly ONE follow-up question.

SKILLS: [{skills_str}]
{coach_block}
JD CONTEXT: {(jd_text or '')[:1800]}

{conversation_block}

PREVIOUS QUESTION: {previous_question}
CANDIDATE'S ANSWER: {previous_answer}

ALREADY ASKED (do NOT repeat same intent):
{avoid_text}

FOLLOW-UP RULES:
1. React to what the candidate actually said — probe gaps, vague claims, or interesting details.
1a. If answer is weak/vague: ask clarifying, practical, and edge-case follow-up.
1b. If answer is strong: escalate to scale, optimization, reliability, or production incident follow-up.
2. Pick a skill angle not yet covered in this conversation thread.
3. Change the question type vs prior questions (try: metrics, failure modes, testing strategy, rollout plan, code review, security, scale).
3a. Keep conversational acknowledgement occasional (at most 1 in 4 follow-ups); usually ask directly.
4. Under 170 characters, single line, no preamble.

Return ONLY the question text."""


# ──────────────────────────────────────────────────────────
#  SYSTEM PROMPT for follow-up (lighter persona)
# ──────────────────────────────────────────────────────────

def build_system_prompt_followup(skills: List[str]) -> str:
    """Build system prompt for adaptive follow-up question generation."""
    skills_str = ", ".join(s.strip() for s in skills[:12] if s.strip()) or "core technical skills"

    return f"""You are a senior technical interviewer conducting a live interview.

BEHAVIOR:
- Listen carefully to the candidate's answer and ask a targeted follow-up.
- Probe deeper into gaps, vague statements, or interesting details.
- Stay anchored to these skills: [{skills_str}]
- Sound like a real interviewer having a conversation, not reading from a script.
- Never repeat a question that was already asked, even rephrased.

OUTPUT: Return exactly one question. No preamble, no explanation. Just the question."""


# ──────────────────────────────────────────────────────────
#  Post-generation validation
# ──────────────────────────────────────────────────────────

_GENERIC_PATTERNS = [
    r"^what is [a-z]",
    r"^explain [a-z]",
    r"^define [a-z]",
    r"^describe [a-z]",
    r"^what are the (?:features|benefits|advantages|types|characteristics) of",
    r"^what (?:do you know|can you tell me) about",
    r"^tell me about [a-z]",
    r"^list (?:the |some )",
    r"^name (?:the |some )",
    r"^what is the (?:purpose|use|role|importance) of",
    r"^how (?:does|do) [a-z]+ work\??$",
    r"^what is the difference between .+ and .+\??$",
    r"^can you explain",
    r"^what are [a-z]+\??$",
]
_GENERIC_RE = [re.compile(p, re.IGNORECASE) for p in _GENERIC_PATTERNS]


def is_generic_question(question: str) -> bool:
    """Detect textbook-style / definition questions that should be rejected."""
    q = (question or "").strip().lower()
    if not q:
        return True
    if len(q) < 15:
        return True
    for pattern in _GENERIC_RE:
        if pattern.search(q):
            return True
    return False


def _skill_token_in_text(skill: str, text: str) -> bool:
    """Check if a skill token appears in text (word-boundary-aware)."""
    s = (skill or "").strip().lower()
    t = (text or "").strip().lower()
    if not s or not t:
        return False
    if s in t:
        return True
    tokens = re.split(r"[\s,;/|+]+", s)
    return all(tok in t for tok in tokens if len(tok) >= 2)


def validate_questions(
    questions: List[str],
    skills: List[str],
    difficulty: str = "medium",
    strict: bool = True,
) -> tuple[List[str], List[str]]:
    """
    Validate generated questions. Returns (accepted, rejected).

    Checks:
    1. Skill alignment — every question must hit at least one required skill
    2. Generic detection — reject "What is X?" patterns
    3. Length sanity — reject empty or ultra-short questions
    4. Duplicate detection — reject near-duplicate questions
    """
    accepted: List[str] = []
    rejected: List[str] = []
    seen_lower: set[str] = set()

    for q in questions:
        q = (q or "").strip()
        if not q or len(q) < 20:
            rejected.append(f"[TOO_SHORT] {q}")
            continue

        low = q.lower()
        if low in seen_lower:
            rejected.append(f"[DUPLICATE] {q}")
            continue

        if strict and is_generic_question(q):
            rejected.append(f"[GENERIC] {q}")
            continue

        if skills and not any(_skill_token_in_text(s, low) for s in skills):
            implied = any(
                _skill_token_in_text(s, low)
                for s in _expand_skill_aliases(skills)
            )
            if not implied:
                rejected.append(f"[OFF_SKILL] {q}")
                continue

        seen_lower.add(low)
        accepted.append(q)

    return accepted, rejected


def _expand_skill_aliases(skills: List[str]) -> List[str]:
    """Expand skills with common aliases to improve validation matching."""
    aliases = {
        "python": ["python", "py", "django", "flask", "fastapi", "pytest", "pip"],
        "sql": ["sql", "query", "database", "db", "postgres", "mysql", "sqlite", "join", "index"],
        "fastapi": ["fastapi", "api", "endpoint", "route", "middleware", "dependency injection"],
        "oops in python": ["class", "inheritance", "polymorphism", "encapsulation", "composition", "abstract", "mixin", "method resolution"],
        "oop": ["class", "inheritance", "polymorphism", "encapsulation", "composition", "abstract", "object"],
        "javascript": ["javascript", "js", "node", "react", "vue", "angular", "typescript", "dom"],
        "react": ["react", "component", "hook", "state", "jsx", "tsx", "redux", "context"],
        "docker": ["docker", "container", "dockerfile", "compose", "image", "volume"],
        "aws": ["aws", "s3", "ec2", "lambda", "cloud", "iam", "rds", "dynamodb"],
        "git": ["git", "branch", "merge", "rebase", "commit", "conflict"],
        "rest": ["rest", "api", "endpoint", "http", "request", "response", "status code"],
        "testing": ["test", "unit test", "integration test", "mock", "fixture", "coverage"],
        "java": ["java", "spring", "jvm", "maven", "gradle", "servlet"],
        "golang": ["go", "golang", "goroutine", "channel", "defer"],
        "redis": ["redis", "cache", "key-value", "pub/sub", "ttl"],
        "mongodb": ["mongodb", "mongo", "document", "nosql", "collection", "aggregation"],
        "kubernetes": ["kubernetes", "k8s", "pod", "deployment", "service", "helm"],
    }
    expanded = list(skills)
    for skill in skills:
        sl = skill.strip().lower()
        for key, vals in aliases.items():
            if sl == key or sl in vals:
                expanded.extend(vals)
    return list(set(expanded))


def rewrite_generic_as_scenario(question: str, skill: str, difficulty: str) -> str:
    """Attempt to rewrite a generic question as a scenario-based one."""
    (question or "").strip()
    sk = (skill or "").strip()
    diff = (difficulty or "medium").strip().lower()

    if diff == "easy":
        return f"Walk through how you'd implement a small feature using {sk} in a real project — what's your first step?"
    if diff == "hard":
        return f"You're debugging a production issue involving {sk} under high traffic — what's your investigation approach?"
    return f"Describe a real situation where you had to make a trade-off decision while working with {sk}."
