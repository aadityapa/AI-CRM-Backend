# The candidate pipeline

The full flow, who owns each stage, and what changed to make it accurate.

**447 tests pass. One migration to run: `0063`.**

---

## The flow

| Stage | Owner | What happens |
|---|---|---|
| Sourcing | TA | Candidate added to an opportunity |
| **Technical Screening** | **TA** | TA runs the **AI L1**. A pass auto-advances and notifies RMG |
| **RMG Review** | **RMG** | RMG reads the report, runs **L2** if wanted, decides fit |
| Sales Screening | Sales | RMG has cleared them; Sales reviews |
| Customer Screening | Sales | Submitted to the customer |
| Customer Interviewing | Sales | The customer is interviewing them |
| **L1 Feedback** | **Sales** | The customer's **first** round verdict is in |
| **L2 Feedback** | **Sales** | The customer's **second** round verdict is in |
| Customer Shortlisted | Sales | Customer has shortlisted them |
| Customer Approved | Sales | Sales attaches rate + joining date, sends for approval |
| → approval | **Sales Head** | Reviews terms, edits if needed, **approves** |
| **Pre Onboarding** | **HR** | HR takes over |
| Joined | HR | Done |

---

## What changed

### Two new stages: L1 Feedback and L2 Feedback

The pipeline jumped from "Customer Interviewing" straight to "Customer
Shortlisted", so there was nowhere to see which of the customer's rounds a
candidate was waiting on, and the first round's feedback had nowhere to live
once the second happened.

They are named for the **feedback**, not the interview, because reaching the
stage means that round's verdict is in — the interview itself happened at
Customer Interviewing.

**Migration `0063`** adds them to the native Postgres enum, inserted *after*
`Customer_Interview` so the enum's own ordering matches pipeline order. It uses
`autocommit_block`, which is required for `ALTER TYPE ... ADD VALUE`, and
`IF NOT EXISTS` so a re-run is safe. Downgrade deliberately raises: Postgres
cannot drop an enum value, and silently reassigning a candidate's stage during
a rollback would be worse than refusing.

### Two ladders both called L1 and L2 — now kept apart

This is the trap in the design, and it is worth being explicit about:

| | When | Who runs it | Where it lives |
|---|---|---|---|
| **L1 Interview / L2 F2F** | before we submit | **RMG** | interview rounds, kinds `L1_Interview` / `L2_F2F` |
| **L1 / L2 Feedback** | after we submit | **the customer**, recorded by Sales | pipeline stages + customer rounds with `stage` = L1 / L2 |

Conflating them would let an engineering verdict be read as the client's. The
round strip on the profile labels them `L1`/`L2` versus `Cust L1`/`Cust L2` for
the same reason, and a test asserts RMG's stages are not inside the customer
ladder.

### Feedback is captured where it is given

Arriving at L1 Feedback or L2 Feedback saves what you type as **that customer
round**, on the Interviews tab, with the round's own `stage`. Closing the
ladder (shortlist / approve / reject) records the verdict against the round.
Recording feedback then a verdict updates the same round rather than creating
two, and an existing write-up is appended to, never overwritten.

Everything else still gets a plain **Reason** box — mandatory, so the activity
log can always explain why something moved.

### AI L1 is TA-only

Scheduling from a resume was already TA-only, but the profile page let RMG and
Sales trigger one too — the same action gave different answers depending on
which screen you were on. `TRIGGER_ROLES` is now `("TA",)` and the UI matches.

### Not every customer runs two rounds

`L1 Feedback → Customer Shortlisted` is allowed directly. Forcing a fictional
L2 stage would make the pipeline describe something that did not happen. Both
feedback stages can also bounce back if a round is re-run, and either can end
in a customer rejection — which is where most rejections actually land.

### Labels

`Preboarding` now reads **Pre Onboarding**, matching how the team says it.
Label only — the stored value is unchanged, as with Customer Shortlisted and
Customer Approved.

---

## To deploy

```bash
cd backend
python -m alembic upgrade head     # applies 0063
```

Then restart. Existing profiles keep their current stage; nothing is
reassigned.

## Verify

```bash
python -m pytest tests/test_full_pipeline_flow.py -q    # 36 passed
```

The suite walks every step of the happy path, asserts the owner of each stage,
that the notified role can actually act on the stage it was told about, and
that Sales still cannot see work sitting with TA or RMG.

---

## Still open

**No status means "customer feedback recorded" as distinct from "in progress"**
— that gap is now much smaller, because L1/L2 Feedback *are* those states for
the customer's rounds. What remains is only the window between an interview
being scheduled and its verdict arriving.

**Bulk actions still have no server endpoint.** The bar counts correctly and
says so honestly.

**The metric tiles describe the current page**, not the whole database. With
3,500 candidates that is misleading enough to deserve a small aggregate
endpoint.
