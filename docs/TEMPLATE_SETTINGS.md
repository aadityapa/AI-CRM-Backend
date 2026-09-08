# Two new template settings

Both live in the existing `job_templates.weights` JSONB blob alongside
`adaptiveNextQuestion`, `enableTimeWarnings` and friends — **no migration
needed**. Set them in the template form (React admin dashboard).

---

## 1. Assess Communication — ON / OFF

*Interview Settings → Assess Communication*

For positions where RMG has judged that proper spoken communication is not a
real requirement, the report covers **technical substance only**.

### What OFF actually does

Turning it off is not a display filter. Three things change:

| | ON | OFF |
|---|---|---|
| Separate communication evaluation | runs | **not called at all** |
| Communication's share of each question's score | 10% | **0%** |
| Per-question rubric | Relevance 30 · Technical 30 · Completeness 20 · Practical 10 · Communication 10 | Relevance 30 · **Technical 35** · **Completeness 25** · Practical 10 |
| Adaptive follow-up difficulty signal | includes communication | technical only |
| Report cards | Communication, Technical, Confidence, Problem solving, Completion | Technical, Problem solving, Completion |
| Radar axes | Comm · Tech · Conf · Solve · Overall | Tech · Solve · Overall |

The rubric change is the point. Hiding the Communication card while leaving
communication at 10% of every question's score would still be marking the
candidate on how they speak — just invisibly. The 10% is redistributed to
technical accuracy and completeness, which is what the toggle is asking the
interview to focus on. Both rubrics still total 100%.

The model is also told explicitly not to penalise broken grammar, hesitation,
accent or awkward phrasing when the technical content is correct.

### Why the report says so

A report with three cards instead of five looks broken. So a banner explains it:

> **Technical assessment only.** This position's template has communication
> assessment switched off, so the candidate was scored purely on technical
> substance.

Confidence disappears along with Communication because it is derived from the
same evaluation — there is no separate confidence measurement to show.

### Existing reports are unaffected

`communication_required` is stamped into the stored report record, and absence
means `true`. Reports created before this setting existed keep rendering all
their scores.

---

## 2. Question order — one by one, or random

*Manual Interview Questions → Question order* (only shown for manual templates)

| | |
|---|---|
| **One by one, in order** | Asked exactly as typed. Use when questions build on each other. |
| **Random order** | Same pool, shuffled per candidate, so parallel sessions do not all open with question 1. |

Shuffling used to be **unconditional** with no way to switch it off, which made
a deliberately-sequenced question list unusable — question 4 could be asked
before question 1.

Verified:

```
sequential       saved-order kept=True   identical-across-candidates=True
                 candidate A sees: Q1 Q2 Q3 Q4 Q5 Q6 Q7 Q8
random           saved-order kept=False  identical-across-candidates=False
                 candidate A sees: Q3 Q1 Q4 Q5 Q6 Q7 Q2 Q8
```

---

## Defaults, and why they matter

Every template saved before today has **no value stored** for either setting, so
both default to the platform's historic behaviour:

- **Communication: assessed.** It always was.
- **Order: random.** It always was.

Getting either default backwards would silently change how live interviews are
scored and ordered for every existing template. Pinned by
`tests/test_template_settings.py`.

One subtlety worth naming: `weights` is form-encoded JSON, so a boolean can
arrive as the **string** `"false"`. Plain `bool("false")` is `True`, which would
have quietly re-enabled communication assessment on every template that turned
it off. The reader handles the string spellings explicitly.

---

## Files changed

| File | Change |
|---|---|
| `backend/utils/template_settings.py` | new — one reader for both settings, shared by the two bootstrap paths |
| `backend/main.py` | stamp onto session meta; gate the shuffle; gate the communication evaluation; thread the flag to the turn scorer; omit comm scores from the summary payload |
| `backend/ai.py` | conditional rubric in the batch and per-turn scorers; `assess_communication` in the response-cache key |
| `backend/hr/service.py` | persist `communication_required` into the report record |
| `TemplateForm.tsx` | Assess Communication toggle; Question order radios |
| `CandidateReportPage.tsx` | banner; hide Communication/Confidence cards; radar and per-turn dimensions |
| `ReportCharts.tsx` | `includeCommunication` on the radar |
| `ProfessionalAssessmentSections.tsx` | `includeCommunication` on the dimension grid |
| `tests/test_template_settings.py` | new — 16 tests |

## Verify

```bash
cd backend && python -m pytest tests/test_template_settings.py -q   # 16 passed
```

No migration and no restart-order dependency — but restart the backend so the
new module loads.
