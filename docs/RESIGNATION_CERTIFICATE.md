# Resignation certificate — where TA uploads it

## Where TA puts the file

**CRM → Candidates → open the candidate → "Upload resignation cert."**

The button sits in the candidate detail header, next to Upload CV. Accepts
PDF, DOC, DOCX, PNG, JPG. Once uploaded the button becomes **Replace resignation
cert.** and a **Resignation certificate** link appears beside it.

Roles allowed: TA, RMG, Sales, Sales_Head, HR (Admin/CEO implicit) — the same set
that can already edit a candidate.

## Why the candidate, not the application

A person resigns from their current job **once**. They may be put forward for five
opportunities; the certificate is the same document each time.

So it is stored on the **candidate** (`candidates.resignation_certificate_url`) and
every Candidate Profile for that person shows it automatically. TA uploads once,
and Sales sees it on every application without asking for the file again.

A profile-level `resignation_certificate_url` also exists (the Zoho export carries
one per profile, 436 rows). If a profile has its own it wins; otherwise the
candidate's is used.

## Where Sales / Sales Head see it

**CRM → Candidate Profiles** — a **Resignation Cert.** column, immediately after
the CV column, showing one of three things:

| Shown | Meaning |
|---|---|
| **View** | Certificate on file — opens in the same preview used for CVs |
| **Not uploaded** (amber) | The candidate is marked resigned but no certificate is attached yet. Hover shows the last working day. |
| **—** | Not resigned |

That middle state is the useful one: it tells Sales the candidate has resigned but
TA has not yet collected the proof, rather than looking identical to "not
resigned".

It also appears in the **Workflow** panel on the profile's Overview tab.

## Endpoints

| Method | Path | Roles |
|---|---|---|
| POST | `/api/candidates/{id}/resignation-certificate` (multipart `file`) | TA, RMG, Sales, Sales_Head, HR |
| DELETE | `/api/candidates/{id}/resignation-certificate` | same |

Uploading also sets `resignation_status = true` if it was not already — handing
over the certificate *is* the statement that they have resigned, so making the
recruiter tick a separate box would only create a way for the two to disagree.

Deleting clears the file but leaves `resignation_status` alone: the candidate may
still have resigned when the wrong document was attached and needs replacing.

Files are served through the existing `/api/crm-files/` route, which is CRM-role
gated and refuses path traversal — the same path CVs use.

## Related fields

`resignation_status` and `last_working_day` are already on the candidate form
under **Separation**, and both show in the Personal information grid. The
certificate completes that set with the actual document.
