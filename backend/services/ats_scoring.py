"""Honest, deterministic ATS resume scorer (pure functions — no DB, fully testable).

Fixes the "random document scores 100%" bug in the previous scorer, which awarded
FULL points for every empty/missing criterion (empty skill pool => full 50, no
optional => full 20, no experience bound => full 15, no location => full 5). A
requirement with no configured skills therefore scored ~90-100% for *any* file.

Design:
  * Gate first — `looks_like_resume` refuses documents that aren't resumes.
  * No free points — score is renormalised over ONLY the criteria that are actually
    configured, so an empty criterion never inflates the score.
  * Fail loud — a requirement with no required skills is a configuration error,
    never a perfect score (`AtsConfigError`).
  * Evidence — every matched skill returns the text snippet proving it.
  * Honest cap — 100% is reachable only when every required skill is matched.
  * JD component — when RMG JD text is present, keyword overlap vs resume is scored
    and weights renormalize; without JD, behavior matches the skills-only scorer.
"""
from __future__ import annotations

import re
from datetime import datetime

# Component weights (out of 100). Only the configured components enter the
# denominator, so missing criteria neither help nor are silently ignored.
MANDATORY_POOL = 50.0
OPTIONAL_POOL = 20.0
EXPERIENCE_POINTS = 15.0
LOCATION_POINTS = 5.0
EDUCATION_POINTS = 10.0
JD_POOL = 20.0

# Default per-component weights. A requirement may override any subset (e.g. an
# experience-heavy role bumps "experience"); missing keys fall back to defaults.
# Only the components actually configured for a requirement enter the denominator,
# so re-weighting never awards points for an absent criterion.
DEFAULT_WEIGHTS: dict[str, float] = {
    "mandatory": MANDATORY_POOL,
    "optional": OPTIONAL_POOL,
    "experience": EXPERIENCE_POINTS,
    "location": LOCATION_POINTS,
    "education": EDUCATION_POINTS,
    "jd": JD_POOL,
}


def resolve_weights(weights: dict | None) -> dict[str, float]:
    """Merge a per-requirement weight override onto the defaults. Non-numeric or
    negative values are ignored so a bad config can never break scoring."""
    w = dict(DEFAULT_WEIGHTS)
    for key, val in (weights or {}).items():
        if key not in w:
            continue
        try:
            fv = float(val)
        except (TypeError, ValueError):
            continue
        if fv >= 0:
            w[key] = fv
    return w


# Common skill spelling/abbreviation equivalences. Conservative on purpose — only
# unambiguous synonyms (not related-but-different techs like C# vs .NET). Matching
# a skill also tries every variant in its group, in either direction.
SKILL_ALIASES: list[frozenset[str]] = [
    frozenset({"javascript", "js", "ecmascript"}),
    frozenset({"typescript", "ts"}),
    frozenset({"react", "reactjs", "react.js"}),
    frozenset({"angular", "angularjs"}),
    frozenset({"vue", "vuejs", "vue.js"}),
    frozenset({"node.js", "nodejs", "node"}),
    frozenset({"kubernetes", "k8s"}),
    frozenset({"golang", "go"}),
    frozenset({"postgresql", "postgres"}),
    frozenset({"c++", "cpp", "cplusplus"}),
    frozenset({"c#", "csharp", "c-sharp"}),
    frozenset({"objective-c", "objectivec", "objc"}),
    frozenset({"github actions", "gh actions"}),
    frozenset({"amazon web services", "aws"}),
    frozenset({"google cloud platform", "gcp"}),
    frozenset({"microsoft azure", "azure"}),
    frozenset({"continuous integration", "ci/cd", "cicd", "ci-cd"}),
]

EDUCATION_KEYWORDS = [
    "B.E", "B.Tech", "M.Tech", "BE", "BTech", "MTech",
    "BSc", "MSc", "MCA", "Bachelor", "Master", "Diploma", "PhD",
]
EXPERIENCE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*\+?\s*(?:years|yrs)", re.IGNORECASE)
# "X years of experience" / "experience: X years" / "total experience X yrs" —
# a candidate's own stated total, which we trust over a bare "N years" mention.
TOTAL_EXP_RE = re.compile(
    r"(?:total|overall|relevant)?\s*(?:experience|exp)\D{0,18}?(\d+(?:\.\d+)?)\s*\+?\s*(?:years|yrs)"
    r"|(\d+(?:\.\d+)?)\s*\+?\s*(?:years|yrs)[^.\n]{0,18}?\b(?:experience|exp)\b",
    re.IGNORECASE,
)
# Employment date ranges: "2019 - 2024", "2019 to Present", "Jan 2020 – 2023".
YEAR_RANGE_RE = re.compile(
    r"\b((?:19|20)\d{2})\s*(?:[-–—]|to|until)\s*"
    r"((?:19|20)\d{2}|present|current|now|till\s*date|date|ongoing|today)\b",
    re.IGNORECASE,
)
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"(?:\+?\d[\s().-]?){8,}\d")
RESUME_SECTION_KEYWORDS = [
    "experience", "education", "skills", "work history", "employment", "project",
    "responsibilit", "curriculum vitae", "resume", "objective", "summary",
    "internship", "certification", "university", "college", "bachelor", "master",
]

#: Phrases that only a JOB DESCRIPTION uses. A JD shares most of its vocabulary
#: with a resume ("experience", "skills", "projects", "responsibilities"), so the
#: section-keyword test alone waves a JD straight through — and a JD scored
#: against its own requirement returns ~100, which looks like a brilliant
#: candidate. These markers are what actually separates the two documents.
_JD_MARKERS = (
    "job description", "job title", "job summary", "job location", "job id",
    "job code", "job type", "job role", "position summary", "position title",
    "role description", "about the role", "about this role", "about the job",
    "basic qualification", "minimum qualification", "preferred qualification",
    "desired qualification", "required qualification", "qualification:",
    "responsibilities:", "key responsibilities", "roles and responsibilities",
    "duties and responsibilities", "what you'll do", "what you will do",
    "we are looking for", "we are seeking", "the ideal candidate",
    "the successful candidate", "candidate should", "candidate must",
    "you will be responsible", "reports to:", "no. of positions",
    "number of positions", "notice period:", "work location:", "experience:",
    "employment type", "equal opportunity employer", "apply now",
)

#: A resume states experience about ITSELF ("4.9 years of experience"); a JD
#: states it as a REQUIREMENT ("5-7 years"). The range form is a JD tell.
_JD_EXPERIENCE_RANGE_RE = re.compile(
    r"\b\d{1,2}\s*[-–to]{1,3}\s*\d{1,2}\s*(?:\+\s*)?(?:years?|yrs?)\b", re.IGNORECASE
)

#: JD scaffolding that says nothing about a candidate. Matching a resume against
#: these was pure noise — a CV does not repeat "Qualification:" or the client's
#: name back at you.
_JD_BOILERPLATE = frozenset({
    # form labels / section headings
    "title", "qualification", "qualifications", "location", "locations",
    "designation", "department", "summary", "overview", "notice", "period",
    "budget", "ctc", "salary", "vacancy", "vacancies", "openings", "opening",
    "minimum", "maximum", "mandatory", "optional", "essential", "desirable",
    "duties", "tasks", "task", "scope", "purpose", "objective", "objectives",
    # generic verbs / filler
    "independently", "closely", "effectively", "efficiently", "successfully",
    "ensure", "ensuring", "perform", "performing", "provide", "providing",
    "support", "supporting", "handle", "handling", "manage", "managing",
    "lead", "leading", "drive", "driving", "own", "owning", "deliver",
    "delivering", "collaborate", "collaboration", "coordinate", "participate",
    "contribute", "assist", "help", "involve", "involved", "responsible",
    # generic business nouns
    "customer", "customers", "stakeholder", "stakeholders", "vendor", "vendors",
    "development", "developments", "activity", "activities", "process",
    "processes", "product", "products", "solution", "solutions", "service",
    "services", "system", "systems", "environment", "quality", "standard",
    "standards", "specification", "specifications", "requirement", "document",
    "documentation", "report", "reports", "review", "reviews", "meeting",
    "meetings", "issue", "issues", "problem", "problems", "improvement",
    "degree", "bachelor", "master", "graduate", "engineering", "engineer",
    "yrs", "month", "months", "week", "day", "days", "full", "time", "shift",
})

#: Common Indian hiring locations — a JD names its city, a resume often does not.
#: Scoring that overlap punishes candidates willing to relocate.
_JD_PLACE_TOKENS = frozenset({
    "bengaluru", "bangalore", "pune", "mumbai", "hyderabad", "chennai", "delhi",
    "gurugram", "gurgaon", "noida", "kolkata", "ahmedabad", "coimbatore",
    "trivandrum", "kochi", "india", "onsite", "offsite", "remote", "hybrid",
})

_JD_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9.+#-]{2,}")
_JD_STOPWORDS = frozenset({
    "the", "and", "for", "with", "this", "that", "from", "into", "onto", "over",
    "under", "about", "above", "below", "after", "before", "between", "among",
    "will", "shall", "must", "should", "would", "could", "have", "has", "had",
    "are", "was", "were", "been", "being", "their", "they", "them", "your",
    "our", "you", "who", "what", "when", "where", "which", "while", "than",
    "then", "also", "such", "other", "only", "both", "each", "few", "more",
    "most", "some", "any", "all", "not", "nor", "but", "per", "via", "using",
    "used", "use", "role", "job", "work", "team", "years", "year", "experience",
    "responsibilities", "requirements", "required", "preferred", "nice", "good",
    "strong", "ability", "able", "knowledge", "understanding", "including",
    "related", "etc", "etcetera", "candidate", "candidates", "position",
    "description", "company", "client", "project", "projects", "based",
})


class AtsConfigError(ValueError):
    """Raised when a requirement is not scorable (e.g. no required skills configured)."""


# Characters that make up a "skill token". A match must not be flanked by these,
# which (unlike \b) correctly bounds terms ending/starting in symbols: C++, C#,
# .NET, Node.js, F#, etc. \b failed on those because a word-boundary can't sit
# between two non-word characters (e.g. the "+" in "C++" and a following space).
_SKILL_CHARS = r"A-Za-z0-9+#._-"

# Boundary set for matching: same as above but WITHOUT the hyphen. A hyphen joins
# two whole words ("high-speed", "cross-functional", "Micro-Controller"), so it is
# a boundary, not part of the word — "high-speed interfaces" genuinely does
# contain "speed", and a JD asking for "speed" should find it. Terms that need
# the hyphen kept still carry it literally in the escaped term itself.
_SKILL_BOUNDARY_CHARS = r"A-Za-z0-9+#._"


def _word_match(term: str, text: str) -> bool:
    if not term:
        return False
    esc = re.escape(term)
    # `.` is in _SKILL_CHARS so "Node.js" and "ASP.NET" stay whole — but that also
    # meant a skill at the END OF A SENTENCE never matched: "experience in
    # Python." failed to match "Python". Allow a following dot unless it is part
    # of a longer token (i.e. followed by an alphanumeric).
    #
    # A hyphen is a compound JOINER, not part of the word: "high-speed interfaces"
    # does contain "speed", and a JD asking for "speed" should find it. So a
    # hyphen is treated as a boundary on both sides — unless the term itself
    # already ends in one. Terms that need the hyphen kept (Micro-Controller)
    # carry it inside `esc` and still have to match literally.
    pattern = rf"(?<![{_SKILL_BOUNDARY_CHARS}]){esc}(?![A-Za-z0-9+#_])(?!\.[A-Za-z0-9])"
    return re.search(pattern, text, re.IGNORECASE) is not None



def _stem(word: str) -> str:
    """Crude singular form, enough for skill matching.

    JDs and resumes disagree on number constantly — a requirement says
    "power supplies" and "oscilloscopes", the resume says "Power Supply" and
    "Oscilloscope". Without this they simply do not match.
    """
    low = word.lower()
    if len(low) < 4:
        return low
    if low.endswith("ies"):
        return low[:-3] + "y"          # batteries -> battery
    if low.endswith("es"):
        # Only drop the whole "es" after a sibilant (boxes -> box, buses -> bus).
        # Otherwise just the "s", or "oscilloscopes" became "oscilloscop".
        stem_body = low[:-2]
        if stem_body.endswith(("s", "x", "z", "ch", "sh")):
            return stem_body
        return low[:-1]                # oscilloscopes -> oscilloscope
    if low.endswith("s") and not low.endswith("ss"):
        return low[:-1]                # generators -> generator
    return low


def _word_match_stemmed(term: str, text: str) -> bool:
    """Word match that also tries the singular/plural counterpart."""
    if _word_match(term, text):
        return True
    stemmed = _stem(term)
    if stemmed != term.lower() and _word_match(stemmed, text):
        return True
    # the other direction: term is singular, resume is plural
    return _word_match(term + "s", text) or _word_match(stemmed + "s", text)


def _skill_variants(term: str) -> list[str]:
    """The term plus any known synonyms/spellings (longest first, so evidence
    prefers the most specific match)."""
    low = (term or "").strip().lower()
    variants = {term.strip()} if term else set()
    for group in SKILL_ALIASES:
        if low in group:
            variants.update(group)
    return sorted(variants, key=len, reverse=True)


#: A multi-word skill counts as present when at least this share of its
#: meaningful words appear in the resume.
PHRASE_MATCH_THRESHOLD = 0.6
#: Words inside a skill phrase that carry no meaning on their own.
_SKILL_PHRASE_NOISE = frozenset({
    "and", "or", "of", "the", "for", "with", "in", "on", "to", "a", "an",
    "design", "designing", "development", "tools", "tool", "based", "using",
})


def _skill_present(term: str, text: str) -> tuple[bool, str | None]:
    """True + the variant that matched when `term` (or a known alias) appears.

    Exact phrase first. Failing that, a MULTI-WORD skill is counted when most of
    its meaningful words appear somewhere in the resume — a requirement written
    as "oscilloscopes function generators" will never appear verbatim, but a
    resume listing "Oscilloscope, Logic Analyzer, Signal Generator" plainly has
    the skill. Requiring the exact phrase marked real skills as missing.
    """
    for variant in _skill_variants(term):
        if _word_match(variant, text):
            return True, variant

    for variant in _skill_variants(term):
        words = [w for w in re.split(r"[\s/,&+]+", variant.lower())
                 if len(w) > 2 and w not in _SKILL_PHRASE_NOISE]
        if not words:
            continue
        # "Micro-Controller design" reduces to one meaningful word once the
        # generic "design" is dropped — still worth matching on.
        hits = [w for w in words if _word_match_stemmed(w, text)]
        if len(hits) / len(words) >= PHRASE_MATCH_THRESHOLD:
            # Report what actually matched, so the breakdown stays honest.
            return True, " + ".join(hits)
    return False, None


def extract_jd_keywords(jd_text: str, max_n: int = 40) -> list[str]:
    """Pull *meaningful* tokens from a JD for overlap scoring.

    The old version took the first 40 tokens that were not in a 97-word stopword
    list, which let through form labels ("Title", "Qualification", "Location"),
    the client's own name ("Aptiv"), the posting city ("Bengaluru") and generic
    filler ("tasks", "independently", "development"). A resume is then penalised
    for not repeating a JD's section headings — which is noise, not signal, and
    it was worth 20% of the score.

    Now: punctuation is stripped, a much larger boilerplate list is filtered, and
    tokens are ranked so genuinely technical terms survive the max_n cut.
    """
    counts: dict[str, int] = {}
    display: dict[str, str] = {}
    for raw in _JD_TOKEN_RE.findall(jd_text or ""):
        token = raw.strip(".,;:()[]{}/\\\"'")   # "specifications." -> "specifications"
        low = token.lower()
        if len(low) < 3 or low.isdigit():
            continue
        if low in _JD_STOPWORDS or low in _JD_BOILERPLATE or low in _JD_PLACE_TOKENS:
            continue
        counts[low] = counts.get(low, 0) + 1
        display.setdefault(low, token)

    def rank(item: tuple[str, int]) -> tuple:
        low, freq = item
        # Prefer terms that look technical: contain a digit/symbol (DDR4, C++),
        # or are repeated — a JD names its real requirements more than once.
        technical = any(c.isdigit() or c in "+#." for c in low)
        return (-int(technical), -freq, low)

    ordered = sorted(counts.items(), key=rank)
    return [display[low] for low, _ in ordered[:max_n]]



#: Experience within this many years of the band still earns partial credit.
EXPERIENCE_TOLERANCE_YEARS = 2.0


def _experience_points(detected: float | None, exp_min: float | None,
                       exp_max: float | None, full: float) -> float:
    """Points for years of experience, tapering outside the band.

    Inside the band            -> full marks.
    Within TOLERANCE below/above -> linear taper down to 40% of full.
    Beyond that                -> 0.
    Unknown                    -> 0 (we cannot credit what we cannot read).

    A candidate 0.1 years short of a 5-year floor is not a 0% match on
    experience, and treating them as one distorts the whole score.
    """
    if detected is None:
        return 0.0
    if (exp_min is None or detected >= exp_min) and (exp_max is None or detected <= exp_max):
        return full
    if exp_min is not None and detected < exp_min:
        gap = exp_min - detected
    elif exp_max is not None and detected > exp_max:
        # Over-qualified is a far smaller concern than under-qualified.
        gap = (detected - exp_max) / 2.0
    else:
        return full
    if gap >= EXPERIENCE_TOLERANCE_YEARS:
        return 0.0
    # 1.0 at no gap -> 0.4 at the tolerance edge.
    factor = 1.0 - (gap / EXPERIENCE_TOLERANCE_YEARS) * 0.6
    return round(full * factor, 2)


def detect_experience_years(text: str) -> float | None:
    """Best estimate of total years of experience, in priority order:

    1. An explicit *stated total* ("X years of experience") — the candidate's own
       figure, preferred over an unrelated "10 years" mentioned elsewhere.
    2. Otherwise the *career span* from employment date ranges (earliest start to
       latest end, "present" = current year) — robust to skill-specific numbers.
    3. Otherwise the largest bare "X years" mention (legacy behaviour).
    """
    text = text or ""

    stated: list[float] = []
    for m in TOTAL_EXP_RE.finditer(text):
        val = m.group(1) or m.group(2)
        if val:
            try:
                stated.append(float(val))
            except ValueError:
                pass
    if stated:
        return max(stated)

    now_year = datetime.now().year
    starts: list[int] = []
    ends: list[int] = []
    for m in YEAR_RANGE_RE.finditer(text):
        try:
            start = int(m.group(1))
        except ValueError:
            continue
        end_raw = m.group(2).lower()
        end = now_year if not end_raw.isdigit() else int(end_raw)
        if 1900 <= start <= now_year and start <= end <= now_year + 1:
            starts.append(start)
            ends.append(end)
    if starts:
        span = max(ends) - min(starts)
        if span >= 0:
            return float(span)

    plain = [float(m.group(1)) for m in EXPERIENCE_RE.finditer(text)]
    return max(plain) if plain else None


def evidence_snippet(term: str, text: str, width: int = 70) -> str | None:
    """Return a short snippet of `text` around the first occurrence of `term`."""
    m = re.search(rf"\b{re.escape(term)}\b", text, re.IGNORECASE)
    if not m:
        return None
    lo = max(0, m.start() - width // 2)
    hi = min(len(text), m.end() + width // 2)
    snippet = re.sub(r"\s+", " ", text[lo:hi]).strip()
    return f"…{snippet}…" if snippet else None


def looks_like_job_description(text: str) -> tuple[bool, list[str]]:
    """Does this document look like a JOB DESCRIPTION rather than a resume?

    Returns (is_jd, the markers found).
    """
    low = (text or "").lower()
    found = [m for m in _JD_MARKERS if m in low]
    if _JD_EXPERIENCE_RANGE_RE.search(low):
        found.append("experience stated as a range")
    return len(found) >= 2, found


def looks_like_resume(text: str) -> tuple[bool, dict]:
    """Heuristic gate: does this document look like a resume/CV?

    A resume identifies a *person*, so contact details are the reliable signal.
    Section keywords are NOT — "experience", "skills", "projects" and
    "responsibilities" are exactly the words a job description uses, so the old
    "two or more sections" rule let a JD through. That mattered: a JD scored
    against its own requirement matches every skill and every JD keyword and
    comes back near 100, which reads as an outstanding candidate.

    So: contact details are accepted on their own; section keywords are accepted
    only when the document does NOT also look like a job description.
    """
    t = (text or "").strip()
    low = t.lower()
    words = t.split()
    has_email = bool(EMAIL_RE.search(t))
    has_phone = bool(PHONE_RE.search(t))
    sections = [k for k in RESUME_SECTION_KEYWORDS if k in low]
    is_jd, jd_markers = looks_like_job_description(t)

    long_enough = len(words) >= 25
    has_contact = has_email or has_phone
    # A JD almost never carries a personal email/phone; a resume almost always
    # does. When contact details are absent, section keywords alone are not
    # enough if the JD markers are present.
    strong_signal = has_contact or (len(sections) >= 2 and not is_jd)
    is_resume = long_enough and strong_signal
    return is_resume, {
        "has_email": has_email,
        "has_phone": has_phone,
        "section_signals": sections[:10],
        "word_count": len(words),
        "looks_like_job_description": is_jd,
        "jd_markers": jd_markers[:10],
    }


def score_resume_against_requirement(
    text: str,
    mandatory_skills: list[str],
    optional_skills: list[str] | None = None,
    exp_min: float | None = None,
    exp_max: float | None = None,
    city: str | None = None,
    jd_text: str | None = None,
    weights: dict | None = None,
) -> dict:
    """Deterministically score resume `text` against a requirement.

    Renormalises over configured criteria only. Skills are OPTIONAL (Aug 2026):
    a requirement with no skills still scores on the RMG JD keyword overlap
    (plus experience/location/education) — TA must be able to scan the moment
    RMG attaches a JD, whether or not skills were entered. AtsConfigError is
    raised only when there is NOTHING to score against (no skills at all AND
    no JD text) — a bare requirement must not yield a meaningless score.
    When `jd_text` is provided, a JD-keyword overlap component is included.
    `weights` optionally overrides the per-component point pools (per requirement).
    Skill matching is alias-aware (React≈ReactJS) and symbol-safe (C++, C#, .NET).
    """
    text = text or ""
    mandatory = [s.strip() for s in (mandatory_skills or []) if s and s.strip()]
    optional = [s.strip() for s in (optional_skills or []) if s and s.strip()]
    jd_keywords = extract_jd_keywords(jd_text) if (jd_text or "").strip() else []
    if not mandatory and not optional and not jd_keywords:
        raise AtsConfigError(
            "Nothing to score against: this requirement has no skills and no RMG JD. "
            "Ask RMG to add the JD (or Skill Evaluation Details) at engineering review."
        )

    w = resolve_weights(weights)
    earned = 0.0
    possible = 0.0
    matched: list[str] = []
    missing: list[str] = []
    evidence: dict[str, str | None] = {}

    # --- mandatory skills (scored only when configured; skills are optional) ---
    m_hit = 0
    if mandatory:
        mand_ok = [(s, *_skill_present(s, text)) for s in mandatory]  # (skill, ok, variant)
        m_hit = sum(1 for _, ok, _v in mand_ok if ok)
        earned += w["mandatory"] * m_hit / len(mandatory)
        possible += w["mandatory"]
        for s, ok, variant in mand_ok:
            (matched if ok else missing).append(s)
            if ok:
                evidence[s] = evidence_snippet(variant or s, text)

    # --- optional skills (only counted when configured) ---
    if optional:
        opt_ok = [(s, *_skill_present(s, text)) for s in optional]
        o_hit = sum(1 for _, ok, _v in opt_ok if ok)
        earned += w["optional"] * o_hit / len(optional)
        possible += w["optional"]
        for s, ok, variant in opt_ok:
            (matched if ok else missing).append(s)
            if ok:
                evidence[s] = evidence_snippet(variant or s, text)

    # --- experience (only when a bound is configured) ---
    detected_years = detect_experience_years(text)
    exp_match: bool | None
    if exp_min is not None or exp_max is not None:
        possible += w["experience"]
        exp_match = (
            detected_years is not None
            and (exp_min is None or detected_years >= exp_min)
            and (exp_max is None or detected_years <= exp_max)
        )
        # Graded, not binary. 4.9 years against a "5-7" band is a rounding
        # difference, yet the old all-or-nothing test scored it zero and took
        # the full 15 points off — the single biggest reason good candidates
        # came out far lower here than a human (or an LLM) would rate them.
        exp_points = _experience_points(detected_years, exp_min, exp_max, w["experience"])
        earned += exp_points
    else:
        exp_match = None  # not a scored criterion

    # --- location (only when configured) ---
    loc_match: bool | None
    if city:
        possible += w["location"]
        loc_match = _word_match(city, text)
        if loc_match:
            earned += w["location"]
    else:
        loc_match = None

    # --- education (a universal resume expectation → always in the denominator) ---
    edu_hits = [k for k in EDUCATION_KEYWORDS if _word_match(k, text)]
    possible += w["education"]
    if edu_hits:
        earned += w["education"]

    # --- RMG JD keyword overlap (only when JD text is present) ---
    # (jd_keywords computed up front — it decides whether scoring is possible.)
    jd_matched: list[str] = []
    jd_missing: list[str] = []
    jd_hit = 0
    if jd_keywords:
        # Prefer tokens not already counted as skill names (still score all JD tokens).
        known = {s.lower() for s in mandatory + optional}
        jd_ok = [(k, _word_match(k, text)) for k in jd_keywords]
        jd_hit = sum(1 for _, ok in jd_ok if ok)
        earned += w["jd"] * jd_hit / len(jd_keywords)
        possible += w["jd"]
        for k, ok in jd_ok:
            (jd_matched if ok else jd_missing).append(k)
            if ok and k.lower() not in known:
                evidence[f"jd:{k}"] = evidence_snippet(k, text)

    total = round(earned / possible * 100, 2) if possible else 0.0
    # With no mandatory skills configured there is no "full required match", so
    # the honest cap below keeps a skill-less requirement from scoring 100.
    all_required = bool(mandatory) and m_hit == len(mandatory)
    # Honest cap: only a full required-skill match may reach 100.
    if not all_required and total >= 100.0:
        total = 99.0

    # Light keyword-stuffing signal: many skills claimed but the doc lacks the
    # structure/length of a real resume.
    _is_resume, sig = looks_like_resume(text)
    stuffed = m_hit >= 3 and sig["word_count"] < 60 and len(sig["section_signals"]) < 2

    score_details: dict = {
        "earned_points": round(earned, 2),
        "possible_points": round(possible, 2),
        "mandatory_matched": m_hit,
        "mandatory_total": len(mandatory),
        "all_required_matched": all_required,
        "detected_experience_years": detected_years,
        "location_match": loc_match,
        "education_keywords_found": edu_hits,
        "jd_applied": bool(jd_keywords),
        "weights": w,
        "weights_customized": bool(weights),
    }
    if jd_keywords:
        score_details.update({
            "jd_keywords_matched": jd_hit,
            "jd_keywords_total": len(jd_keywords),
            "jd_match_ratio": round(jd_hit / len(jd_keywords), 4),
            "jd_pool_points": w["jd"],
        })

    breakdown: dict = {
        "skills_matched": matched,
        "skills_missing": missing,
        "evidence": evidence,
        "experience_match": exp_match,
        "keyword_stuffing_suspected": stuffed,
        "score_details": score_details,
    }
    if jd_keywords:
        breakdown["jd_keywords_matched"] = jd_matched
        breakdown["jd_keywords_missing"] = jd_missing

    return {
        "valid": True,
        "ats_score": total,
        "breakdown": breakdown,
    }
