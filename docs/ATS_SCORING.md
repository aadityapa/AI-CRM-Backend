# ATS scoring — how it works, and what was wrong

Written after a real report: a hardware engineer's resume scored **24/100** here
while ChatGPT scored the same resume + JD at **84%**. That gap was real. This
document records what caused it, what changed, and how to check it yourself.

---

## The verdict

The 24 was wrong. After the fixes the same resume scores **79 deterministic /
81 blended** — which is the right neighbourhood for a candidate ChatGPT put at 84.

|  | Before | After |
|---|---:|---:|
| Deterministic (keyword + criteria) | 24.0 | 79.0 |
| With the AI semantic blend | 24.0 | **81.0** |
| ChatGPT, same inputs | — | 84 |

The remaining 3-point difference is one genuinely missing skill (**Functional
Safety / ISO 26262**), which ChatGPT also flagged as a gap. Agreeing on the gap
is the correct outcome — the score should not be 84 if the candidate really is
missing a required skill.

---

## How the score is built

```
final = 0.6 × deterministic + 0.4 × AI semantic
```

The deterministic half is a fixed rubric, renormalised over whatever the
requirement actually configures (a requirement with no location set does not
lose location points — the denominator shrinks instead):

| Component | Weight |
|---|---:|
| Required skills matched | 50 |
| JD keywords found in the resume | 20 |
| Experience within the band | 15 |
| Education keywords | 10 |
| Location match | 5 |

The AI half is a `gpt-4o-mini` call that reads the JD and the resume and returns
a `match_percent` with strengths and gaps. It exists because keyword matching
cannot know that "AUTOSAR stack work" implies embedded C, or that "Signal
Generator" is what the JD means by "function generators".

**A cap is preserved:** a candidate missing a required skill can never reach 100,
even if the blend maths would get there. It is clamped to 99.

---

## The five causes of the 24

### 1. The AI half was failing silently — the single biggest factor

`_ai_semantic_review()` returned `None` on any exception, and `run_ats_scan`
treated `None` as "skip the blend". So a revoked key, an expired billing card, or
a blocked outbound connection turned a 60/40 blend into a keyword-only score,
**with nothing anywhere in the UI saying so**. The number just looked wrong.

Fixed three ways:

- the failure reason is now returned instead of `None`
- `breakdown.score_details.ai_unavailable_reason` records it
- the ATS breakdown modal shows an amber **"Partial score"** banner naming the reason

Run `python scripts/diagnose_ats.py` to test the key with a live call.

### 2. Skills at the end of a sentence never matched — a pre-existing bug

`_SKILL_CHARS` includes `.` so that `Node.js` and `ASP.NET` match as single
tokens. The side effect: the negative lookahead also rejected a skill followed
by a full stop. `"Strong experience in Python."` did **not** match `Python`.

Any resume that ends a bullet with the skill lost that skill. Fixed with a
lookahead that only treats `.` as part of the token when a letter or digit
follows it:

```python
pattern = rf"(?<![{_SKILL_CHARS}]){esc}(?![A-Za-z0-9+#_-])(?!\.[A-Za-z0-9])"
```

`Node.js` still matches whole, and `Node` still does **not** match inside
`Node.js` — verified by tests.

### 3. JD keyword extraction was scoring boilerplate — 20% of the score

The extractor pulled capitalised and technical-looking tokens out of the JD with
no filtering. On the real JD that meant it was "requiring" the resume to contain:

> Title · Qualification · Location · Aptiv · Bengaluru · degree · minimum ·
> independently · tasks · lead · customer · development · products · `specifications.`

Note the trailing period — punctuation was not stripped, so `specifications.`
could never match the word `specifications`.

Fixed: punctuation stripped, plus a ~90-word `_JD_BOILERPLATE` list (structural
words, HR verbs) and a `_JD_PLACE_TOKENS` list (Indian city names). Genuine terms
— `DDR4`, `LPDDR5`, `PCIe`, `MIPI`, `USB3.0` — are kept and still scored.

### 4. Experience was binary — 4.9 years against a 5-7 band scored 0/15

One month short of the band and the candidate lost the entire component. The
band is a preference, not a cliff.

Now graduated over a 2-year tolerance, and being **over** the band is penalised
at half the rate of being under (an over-qualified candidate is a different
problem from an under-qualified one):

| Detected | Band | Before | After |
|---:|:---:|---:|---:|
| 4.9 | 5-7 | 0 / 15 | **13.6 / 15** |
| 4.0 | 5-7 | 0 / 15 | 6.0 / 15 |
| 3.0 | 5-7 | 0 / 15 | 0 / 15 |
| 9.0 | 5-7 | 0 / 15 | 8.5 / 15 |

### 5. Multi-word skills required an exact phrase

The requirement `"oscilloscopes function generators"` was matched literally. A
resume saying *"Oscilloscope, Logic Analyzer, Signal Generator"* scored zero on
it, despite obviously having the skill.

Now a multi-word skill matches when **60% of its meaningful words** are present,
with a light plural stemmer. Noise words are dropped first, and hyphenated skills
are **not** split — `Micro-Controller design` stays intact.

The stemmer is deliberately conservative (`oscilloscopes → oscilloscope`, not
`oscilloscop`) so it cannot create false matches.

---

## Did this just inflate every score?

That was the risk, so it is tested explicitly. An unrelated resume — a Chartered
Accountant against the same embedded-hardware JD — still scores **under 40**, and
`Functional Safety` is still correctly reported missing from the matching
candidate.

`tests/test_ats_scoring_accuracy.py` pins each fix, including the guards:

```
python -m pytest tests/test_ats_scoring.py tests/test_ats_scoring_accuracy.py -q
37 passed
```

---

## Round 2 — the 91/100 was not a real score

After the fixes above, the same candidate came back at **91/100** with
`Experience: Match (7 yrs)`, `Required skills 5/5`, `JD keywords 40/40` and
`Missing skills: None`.

None of that is true of the resume. Bibin's CV says **4.9 years**, and it is a
consumer-camera engineer's CV — body-worn camera, baby monitor, hunting camera.
It contains no "Functional Safety", no "ISO26262", no "EMC", no "Automotive".
Yet all of those showed as matched.

**Cause: the job description had been uploaded into the resume slot.** The scan
was scoring the JD against its own requirement.

Feeding the JD in as the resume reproduces the report almost exactly:

| | Screenshot | JD scored as its own resume |
|---|---:|---:|
| Keyword / criteria | 95 | 95 (100 − 5 for the location miss) |
| Experience detected | 7 yrs | 7.0 — read off the JD's own "5-7 years" |
| Required skills | 5 / 5 | 5 / 5 |
| JD keywords | 40 / 40 | 40 / 40 |
| Missing skills | None | None |
| Education | BE | `['BE']` — from "BE degree minimum" |

Every field lines up. The AI reviewer's "the candidate shows familiarity with
functional safety and EMC compliance" was it describing the job description.

### Why the guard didn't catch it

`looks_like_resume()` accepted any document with **two or more** of
`experience`, `education`, `skills`, `project`, `responsibilit`… — which is a
description of a job description. The JD scored four of them and sailed through.

### Two guards added

1. **`looks_like_job_description()`** — 40-odd phrases only a JD uses
   ("job title", "basic qualification", "responsibilities:", "we are looking
   for", "reports to:"), plus "experience stated as a range", which is the
   difference between a CV saying *"4.9 years of experience"* and a JD saying
   *"5-7 years"*. Two markers and the document is treated as a JD.

   `looks_like_resume()` now accepts section keywords **only when the document
   does not also look like a JD**. Contact details (email/phone) still pass on
   their own, so anonymised CVs are unaffected.

2. **A self-match check in `run_ats_scan`** — Jaccard word overlap between the
   "resume" and the requirement's JD. Over 85% and the scan is refused. A real
   CV overlaps the JD by 10-25%; a copy of the JD overlaps ~100%.

Both return 422 with an explanatory message rather than a number.

### The honest score for this pairing

| | |
|---|---:|
| Deterministic | **51.6 / 100** |
| Required skills | 2 / 5 |
| Missing | Functional Safety, Embedded, Micro-Controller design |
| Experience | 4.9 yrs against a 5-7 band |
| JD keywords | 14 / 40 |

That is the correct answer. The resume is a competent high-speed hardware
engineer, but for **consumer** camera products — the JD is **automotive**, wants
ISO 26262 functional safety, EMC compliance and micro-controller design, and the
candidate is four months short of the band. ChatGPT's 84 was generous; it was
reading transferable skill, not literal requirement coverage.

### Also fixed in this round

`_word_match` treated a hyphen as part of a word, so `"speed"` did not match
`"high-speed interfaces"`. A hyphen joins two whole words and is now a boundary
on both sides. `Node.js`, `C++` and `Micro-Controller` all still behave
correctly — pinned by tests.

---

## Checking it on your side

```bash
cd backend

# Is the AI half actually working? (makes one live OpenAI call)
python scripts/diagnose_ats.py

# Re-score a specific resume and see where every point went
python scripts/diagnose_ats.py --resume-id 123
```

Then **re-run the ATS scan** on the candidate from the screenshot. Existing
`ats_score` values were computed with the old logic and are stored on the row —
they do not update until the scan is run again. Expect roughly 24 → 81.

If `diagnose_ats.py` reports the live call failing, that is the remaining gap and
it is a credentials/billing issue rather than a code one. The score will now say
so in the UI instead of quietly reading low.

---

## Files changed

| File | Change |
|---|---|
| `backend/services/ats_scoring.py` | word-boundary fix, hyphen-as-boundary, stemmer, phrase matching, graduated experience, JD boilerplate filter, `looks_like_job_description()` + tightened `looks_like_resume()` |
| `backend/services/resumes.py` | AI review reports its failure reason instead of `None`; JD-uploaded-as-resume rejected with 422; `_texts_are_near_identical()` self-match guard |
| `backend/tests/test_ats_scoring_accuracy.py` | new — 27 regression tests |
| `backend/scripts/diagnose_ats.py` | new — key check + per-resume score explanation |
| `frontend/.../crm/pages/Requirements.tsx` | "Partial score" banner when the AI half did not run |
