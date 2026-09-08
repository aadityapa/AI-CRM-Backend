# Zoho candidates import — runbook

`candidates.json` (Zoho NEXUS candidate export, 6,719 records) is the richest
candidate source available and a **superset** of `all_candidates_clean.csv`:

| | |
|---|---|
| Records | 6,719 |
| Covers every candidate the applications file references | **6,709 / 6,709** (the CSV was missing 16) |
| Brand new candidates not in the old CSV | 26 |
| Candidates in the CSV but not here | 0 |

Importing it first takes the application linkage from 99.8% to **100%**.

## Run order

```
cd backend

python -m alembic upgrade head                                   # applies 0057

# 1. candidate master  (point --cv-dir at the folder of <candidate_id>.pdf files)
python tools/import_candidates_json.py --cv-dir "F:\path\to\resumes"
python tools/import_candidates_json.py --cv-dir "F:\path\to\resumes" --apply

# 2. opportunities/customers, if not already loaded
python tools/replace_customers.py --apply
python tools/import_opportunities_full.py --apply

# 3. applications + interview history
python tools/import_applied_opportunities.py
python tools/import_applied_opportunities.py --apply --replace
```

Both importers dry-run by default and print exactly what they would do.

Omit `--cv-dir` and only the original filename is recorded; re-run later with it
and the CV links fill in.

## Resumes

The export names every file `<candidate_id>.<ext>` and carries the **real**
extension, which matches the folder exactly:

| Type | Count | Preview |
|---|---|---|
| `.pdf` | 5,477 | rendered inline via pdf.js |
| `.docx` | 954 | rendered inline via mammoth |
| `.doc` | 62 | download only — the old binary Word format has no in-browser renderer |
| `.html`, `.pptx` | 2 | download only |
| **Total** | **6,495** | |

Matching order: exact filename → `<zoho_candidate_id>.<any extension>`. The
folder is indexed **recursively and case-insensitively**, so batch subfolders,
`.PDF` vs `.pdf`, and files whose extension was changed on disk all still link.

### ZIP archives — no extraction needed

`--cv-dir` accepts loose files, `.zip` archives, or a mix. Archives are **read in
place**; nothing is unpacked to disk, so a folder of 15 zips works directly:

```
F:\resumes\
   resumes_part01.zip   (433 files)
   resumes_part02.zip   (433 files)
   ...
   resumes_part15.zip   (433 files)
```

```
python scripts/link_candidate_resumes.py --cv-dir "F:\resumes"          # dry run
python scripts/link_candidate_resumes.py --cv-dir "F:\resumes" --apply
```

Folders nested inside an archive are searched, and `__MACOSX/`, `._*` resource
forks and `Thumbs.db` are ignored. If the same filename appears in two archives
the first wins; a loose file on disk always beats a copy inside a zip, so you can
drop in a corrected resume without rebuilding the archive.

Verified against 15 generated zips of 433 files each, with a nested folder,
mixed-case names and junk entries: **6,495 / 6,495 matched**, split correctly as
5,477 pdf · 954 docx · 62 doc · 1 html · 1 pptx.

Files are copied into `data/crm_uploads/cv/` under a hash of the source filename,
keeping the real extension — so re-running overwrites rather than accumulating
duplicates. They are served by `GET /api/crm-files/cv/<name>`, which is CRM-role
gated and refuses path traversal.

### Linking resumes on their own

If the candidates are already imported, use the focused script instead of
re-running the whole import — it only touches `cv_url` and
`cv_original_filename`:

```
python scripts/link_candidate_resumes.py --cv-dir "F:\path\to\resumes"
python scripts/link_candidate_resumes.py --cv-dir "F:\path\to\resumes" --apply
```

Add `--relink` to replace CV links that are already set. Candidates with no
matching file are written to `candidates_without_resume.csv`, and the script
reports any files in the folder that no candidate references.

## Expected result

| | Before | After |
|---|---|---|
| Candidates unresolved in the applications file | 17 | **0** |
| `candidate_profiles` rows | 7,966 | **7,983** |
| Opportunities showing applicants | 85 | 85 |
| Candidates linked to an opportunity | 6,530 | **6,547** |

9 applications are still skipped — their `opportunity_id` does not exist in
`sales_opportunities_full.csv`. They are listed in the skipped-rows CSV.

## Upsert rules

`zoho_candidate_id` → `email` → create new. **Nothing is ever deleted** —
candidates created in the app that are absent from this file are left untouched,
exactly as requested.

`zoho_candidate_id` is the important addition. Previously the applications import
bridged Zoho id → CSV → email → candidate; now it resolves directly, so re-imports
stay correct even after someone edits an email in the app.

## Data handling

| Issue in the export | Handling |
|---|---|
| 4,691 records carry the whole name in `first_name`, `last_name` blank | Split on whitespace; 3+ tokens become first/middle/last. Salutation taken from `prefix`. |
| 12 `experience_years` values are pasted phone numbers (e.g. 9182651901) | Anything outside 0–60 is dropped rather than stored |
| CTC is lakhs for 4,518 rows, rupees for 14 | ≤ 500 → ×100,000; above that taken as rupees |
| `notice_period` is numeric ("15.00") | → "15 days"; "0.00" → "Immediate" |
| 7 duplicate + 4 blank emails vs the unique email key | 11 deterministic placeholders `<name>.<sha8>@import.karnex.in`. No candidate lost; all stay linkable by Zoho id. Listed in the report CSV. |
| 165 candidates have 2–6 preferred locations | Full list in `preferred_locations`; the first also populates `preferred_location_id` so existing filters keep working |
| `skills`, `preferred_locations`, `roles` | Upserted into the Skills / Locations / Designations masters |

## Schema (migration 0057)

Added to `candidates`:

| Column | Purpose |
|---|---|
| `zoho_candidate_id` | durable external id, partial-unique (app-created candidates have none) |
| `city` | candidate's current city |
| `preferred_locations` | full comma-separated list |
| `recruiter_email` | owning recruiter from Zoho |
| `cv_original_filename` | original CV name, shown even when the PDF isn't loaded |
| `source_created_date` | when the record was created in Zoho |

## UI

The candidate detail page previously showed 15 fields while the form saved 23 —
salutation, middle name, date of birth, gender, experience, notice period, roles
and **current CTC** were all captured and stored but never rendered back. The page
now shows every stored field, plus the new ones (city, preferred locations,
recruiter, CV filename, Zoho ID, added-in-Zoho date).

The list gained City, Exp (yrs) and Current CTC columns. City, Recruiter and
Preferred locations are editable in the candidate form.

## How the four tabs line up afterwards

```
Customer ── Opportunity ── Requirement ── Resume
                 │                          │
                 └── Candidate Profile ── Candidate
                     (pipeline, CTC,       (person master,
                      interview rounds,     unique email +
                      offers, AI sessions)  zoho_candidate_id)
```

* **Opportunity → Applicants** lists every candidate who applied (7,983 rows across 85 opportunities).
* **Candidate → Linked Profiles** shows every opportunity that candidate applied to, with the opportunity code, customer, pipeline status, expected CTC, TA owner, applied date and interview-round count.
* **Candidate Profile → Interviews** shows every round with date, mode, interviewer, result and full feedback; **Offers**, **Skill Evaluation**, **Activity Log** and **AI Interview** carry the rest.
