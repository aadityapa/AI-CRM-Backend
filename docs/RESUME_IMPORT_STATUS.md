# Resume import — status and next step

## What I found in the 15 archives

All 15 zips read cleanly: **6,495 files, zero duplicates**, a perfect 1:1 with the
`resume_file` names in `candidates.json`. (They are not 433 each — `resumes_01.zip`
holds 4,638 and the rest 98–164, but the total is exact.)

**However, only 2,006 are real resumes.**

| | Count | |
|---|---:|---|
| Genuine documents | **2,006** | 1,766 pdf · 219 docx · 21 doc |
| Zoho API-limit stubs | **4,489** | not resumes — see below |
| **Total** | 6,495 | |

### The 4,489 bad files

Every one is exactly 120 bytes and contains this, saved with a `.pdf` or `.docx`
extension:

```json
{"code": 4000,
 "message": "Account's Developer API limit has been reached. Please upgrade to execute more REST API calls."}
```

The Zoho bulk download hit the developer API rate limit and wrote the **error
response body** to the file instead of the document. They are spread across all
archives, so re-downloading a single zip will not fix it.

Nothing was lost — the resumes still exist in Zoho. They need downloading again,
slower or on a higher API tier.

## What is already done

- The **2,006 genuine resumes are copied into `data/crm_uploads/cv/`** (417 MB),
  named exactly as the app expects. Verified: 2,006 files on disk, none under 1 KB.
- `import_templates/resume_manifest.csv` — 2,006 rows mapping Zoho candidate id →
  `cv_url`, ready for the database.
- `import_templates/resumes_to_redownload.csv` — the 4,489 candidates whose CV
  needs fetching again, with name, email and expected filename.

## Fixed after the first live run: duplicate-email collapse

The first `import_candidates_json.py --apply` reported only **4** synthesised
emails when **11** were expected (4 blank + 7 duplicated). Cause: the placeholder
branch only fired when no candidate matched at all. For the 7 pairs that share an
email, record A matched an existing row, then record B matched *that same row* and
overwrote it — losing 7 candidates and leaving 7 others holding the wrong person's
data.

`import_candidates_json.py` now tracks which row each Zoho id claims during a run.
A row already claimed by a different Zoho id is not treated as a match, so the
second record becomes a new candidate with a placeholder email.

Simulated against the exact database state that run produced:

| | Rows | Zoho ids stored | Lost |
|---|---:|---:|---:|
| Old code (what ran) | 6,712 | 6,712 / 6,719 | **7** |
| Fixed, re-run on that database | 6,719 | 6,719 / 6,719 | **0** |

**Re-run the candidate import to repair it** — it creates exactly the 7 missing
rows and changes nothing else.

## Where to see a resume in the UI

**Candidates tab → click any candidate → "View CV"** in the header card.
It opens in a modal: PDFs render inline via pdf.js, DOCX via mammoth, and `.doc`
offers a Download button (the old binary Word format has no browser renderer).

**Candidate Profiles → open a profile → "View CV"** next to the email/phone line
in the header — added because the payload always carried `cv_url` but the page
never rendered it, so RMG could not open a resume while reviewing an application.

### Finding the 2,006 that have one

Only ~30% of candidates have a CV on file, so the Candidates list now has:

* a **CV** column — "Yes" with the original filename on hover, or "—"
* a **CV: any / Has CV / No CV** filter next to the skill filter

Backed by `GET /api/candidates?has_cv=true|false`.

## Your next step

The files are in place; only the database link remains:

```
cd backend
python scripts/link_candidate_resumes.py --manifest ../import_templates/resume_manifest.csv
python scripts/link_candidate_resumes.py --manifest ../import_templates/resume_manifest.csv --apply
```

Manifest mode needs no source folder and re-checks that each file is really on
disk before pointing a candidate at it.

Run the candidate import first if you have not — the manifest matches on
`zoho_candidate_id`:

```
python tools/import_candidates_json.py --apply
```

## After you re-download the missing 4,489

Point the linker at the new folder — zips or loose files, either works:

```
python scripts/link_candidate_resumes.py --cv-dir "F:\resumes_v2" --apply
```

Candidates already linked are skipped, so only the gaps fill in. Add `--relink`
to replace existing links too.

## The guard that is now in place

Both importers run every candidate file through `resume_problem()` before linking
it. A file is **rejected, not linked**, when it is:

- a Zoho `{"code", "message"}` error payload
- any other JSON where a document was expected
- empty, or under 512 bytes with no recognisable document signature

Real formats are recognised by magic bytes rather than extension: `%PDF`, `PK`
(docx/pptx), OLE (`.doc`), RTF, and HTML (Naukri exports HTML resumes with a
`.doc` extension — 14 of those are genuine and were kept).

Rejected files are listed in the run's report CSV. This means a future
rate-limited download can never silently give candidates a "View CV" button that
opens an error page.

## Performance note

Reading 6,500 members was reopening each 27 MB archive every time. Archive
handles are now cached and closed at the end, which took the scan from minutes to
seconds.
