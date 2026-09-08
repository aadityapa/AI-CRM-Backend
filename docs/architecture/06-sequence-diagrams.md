# 6. Sequence Diagrams (key workflows)

## 6.1 Authentication (register + login + JWT)

```mermaid
sequenceDiagram
    autonumber
    participant UI as Frontend (auth/hrAuth.js)
    participant API as main.py auth routes
    participant ADB as auth_db.py
    participant DB as PostgreSQL

    UI->>API: POST /auth/register {username,email,password,role}
    API->>API: gate public HR registration (_allow_public_hr_registration)
    API->>ADB: register_user (PBKDF2-HMAC-SHA256, 120k iters)
    ADB->>DB: INSERT registration_data
    API-->>UI: 201 created

    UI->>API: POST /auth/login {username|email, password}  (rate-limit 10/min)
    API->>ADB: verify_login (re-hash with stored salt)
    ADB->>DB: SELECT user, then INSERT login_data (audit)
    API->>API: _issue_access_token (HS256: sub, role, email, exp)
    API-->>UI: 200 {access_token, expires_at_ist, user}
    UI->>UI: saveAuthSession (localStorage authToken)
```

## 6.2 HR schedules interview + invite email

```mermaid
sequenceDiagram
    autonumber
    participant HR as HR UI (hr.js)
    participant API as main.py /hr/schedule-interview
    participant ADB as auth_db.py
    participant Bridge as ai_interview_bridge.py
    participant SMTP as email_smtp.py
    participant DB as PostgreSQL

    HR->>API: POST /hr/schedule-interview {candidate, job template, [crm ids]}
    API->>API: _require_user({"hr"})
    API->>API: _pack_invite_config_into_notes (skills, timing, model...)
    API->>ADB: create_interview_schedule
    ADB->>DB: INSERT interview_schedule (invite_token, access_key)
    opt CRM linkage (candidate_id + opportunity_id)
        API->>Bridge: link_schedule_to_crm
        Bridge->>DB: INSERT ai_interview_links (result=Pending)
    end
    opt SMTP enabled
        API->>SMTP: send_interview_invite_email (link + access_key)
        SMTP-->>API: {ok}
    end
    API-->>HR: 200 {invite_url, access_key, email_sent}
```

## 6.3 Candidate interview loop (the core workflow)

```mermaid
sequenceDiagram
    autonumber
    participant C as Candidate UI (candidate.js)
    participant API as main.py interview routes
    participant OAI as OpenAI
    participant SESS as session.py (memory)
    participant DB as PostgreSQL

    C->>API: GET /candidate/invite/{token}
    API->>DB: get_schedule_by_token (+ prewarm session)
    API->>OAI: (background) generate questions
    C->>API: POST /candidate/invite/{token}/verify {email, access_key}
    API->>DB: bind active_device_id, session_status=verified
    C->>API: POST /candidate/invite/{token}/login
    API->>API: _bootstrap_invite_interview_session (fast_only)
    API->>SESS: store sessions["inv:token"] (+ warmup injected)
    API-->>C: candidate JWT (carries invite_token)

    loop each question
        C->>API: GET /next
        API->>SESS: resolve session (recover from interview_progress if lost)
        API-->>C: question payload (text, index, timing)
        C->>API: POST /candidate/tts
        API->>OAI: TTS synth
        OAI-->>C: audio (mic muted while playing)
        C->>C: VAD (silero) + record speech
        C->>API: POST /candidate/transcribe
        API->>OAI: Whisper STT
        OAI-->>C: transcript
        C->>API: POST /answer {transcript}
        API->>API: speech_validation + auto_advance rules
        API->>OAI: (background) _schedule_turn_evaluation
        API->>DB: persist interview_progress
        API-->>C: next question / done
    end

    par Proctoring (parallel)
        C->>API: POST /interview/violation (tab switch, multi-face)
        API->>DB: violations_log++
    end

    C->>API: POST /submit
    API->>API: _finalize_interview_snapshot
    API->>OAI: _evaluate_and_store_report (score, strengths/weaknesses)
    API->>DB: upsert interview_records + interview_schedule=completed
    API->>API: _crm_sync_interview_async (see 6.5)
    API-->>C: redirect thank-you / terminated
```

## 6.4 HR reviews results & records decision

```mermaid
sequenceDiagram
    autonumber
    participant HR as HR UI (results.js)
    participant API as main.py /hr/*
    participant ADB as auth_db.py
    participant U as utils (score_exclusion, strengths_weaknesses)
    participant DB as PostgreSQL

    HR->>API: GET /hr/dashboard, GET /hr-records
    API->>DB: read interview_records
    HR->>API: POST /report {secret}
    API->>API: verify report code
    API-->>HR: report payload
    HR->>API: PATCH .../per-question/{q}/score-exclusion
    API->>U: exclude/include question, recompute aggregates
    API->>DB: update record (audit trail)
    HR->>API: PUT /hr/candidates/{cid}/hr-decision {shortlist|reject|on_hold}
    API->>ADB: set_hr_candidate_decision
    ADB->>DB: upsert hr_candidate_decisions + mirror to record
    API-->>HR: 200 (dashboard cache invalidated)
```

## 6.5 CRM ↔ Interview bridge (trigger L1 + sync back)

```mermaid
sequenceDiagram
    autonumber
    participant TA as CRM UI (Profiles/Resumes)
    participant CR as routers/crm/ai_interviews.py
    participant Bridge as ai_interview_bridge.py
    participant ADB as auth_db.py
    participant IntAPI as Interview platform (main.py)
    participant Notify as services/notify.py
    participant DB as PostgreSQL

    TA->>CR: POST /api/candidate-profiles/{id}/ai-interviews
    CR->>CR: role_required(TA,RMG,Sales), 409 if Pending exists
    CR->>Bridge: schedule_l1_interview (skills, config)
    Bridge->>ADB: create_interview_schedule (hr_username=karnex-crm)
    ADB->>DB: INSERT interview_schedule
    Bridge->>DB: INSERT ai_interview_links (result=Pending)
    Bridge->>DB: log AI_INTERVIEW_SCHEDULED (activity)
    CR-->>TA: invite_url + access_key

    Note over IntAPI: Candidate completes interview (6.3)
    IntAPI->>Bridge: sync_completed_interview (via _crm_sync_interview_async)
    Bridge->>DB: find ai_interview_links by invite_token
    Bridge->>DB: set result Passed/Failed (vs pass threshold), completed_at
    Bridge->>DB: set resumes.ai_interview_status
    Bridge->>DB: write skill_evaluations.reviewer_rated
    Bridge->>Notify: notify_role(TA)
    Notify->>DB: INSERT notifications
```

## 6.6 Requirement approval state machine

```mermaid
stateDiagram-v2
    [*] --> Draft
    Draft --> Pending_Sales_Head_Approval: submit (Sales)
    Pending_Sales_Head_Approval --> Sales_Head_Rejected: reject (Sales_Head)
    Pending_Sales_Head_Approval --> Pending_Engineering_Review: approve (Sales_Head)
    Pending_Engineering_Review --> Engineering_Rejected: reject (RMG)
    Pending_Engineering_Review --> Open_For_Sourcing: approve (RMG)
    Open_For_Sourcing --> Posted_On_Portals: add job posting (TA)
    Posted_On_Portals --> In_Progress
    In_Progress --> Fulfilled
    Open_For_Sourcing --> Closed: close (Sales_Head)
    Open_For_Sourcing --> Cancelled: cancel (Sales_Head)
    Sales_Head_Rejected --> [*]
    Engineering_Rejected --> [*]
    Fulfilled --> [*]
    Closed --> [*]
    Cancelled --> [*]
```

## 6.7 Opportunity & candidate-profile pipelines

```mermaid
stateDiagram-v2
    direction LR
    state "Opportunity pipeline_stage" as OPP {
        [*] --> New
        New --> Active
        New --> On_Hold
        New --> Rejected
        Active --> On_Hold
        Active --> Closed_Won
        Active --> Closed_Lost
        Active --> Closed_Partial
        Active --> Rejected
        On_Hold --> Active
        On_Hold --> Closed_Lost
        On_Hold --> Closed_Partial
        On_Hold --> Rejected
        Closed_Won --> Archived
        Closed_Lost --> Archived
        Closed_Partial --> Archived
        Rejected --> Archived
        Archived --> [*]
    }
```

```mermaid
stateDiagram-v2
    direction LR
    state "CandidateProfile pipeline_status" as CP {
        [*] --> Sourcing
        Sourcing --> Technical_Screening
        Technical_Screening --> RMG_Review
        RMG_Review --> Sales_Screening
        Sales_Screening --> Customer_Screening
        Customer_Screening --> Customer_Interview
        Customer_Interview --> Shortlisted
        Shortlisted --> Customer_Approval
        Customer_Approval --> Preboarding
        Preboarding --> Joined
        Joined --> [*]
    }
```

## 6.8 Project Employee bridge (map → leave → invoice)

```mermaid
sequenceDiagram
    actor HR as HR / Sales_Head
    participant API as /api/projects
    participant PE as project_employees
    participant Leave as leave_details
    participant PO as po_project_allocations
    participant TS as timesheets

    HR->>API: POST /{project_id}/employees
    API->>PE: insert / reactivate mapping
    API->>Leave: seed copy from CustomerLeavePolicy
    API->>PE: ensure project_employee_rates (is_current)
    HR->>API: POST /leave-applications (project_id / project_employee_id required when mapped)
    Note over API,Leave: Approve debits PE leave_balance only; Comp-Off bypasses balance gate
    HR->>TS: create/submit timesheet
    Note over TS,PO: Submit never blocked by PO; calendar holidays locked on entries
    HR->>API: generate-invoice from timesheet
    API->>PE: split_period_by_rate times billable
    API->>PO: drawdown consumed_amount (block if 100%/expired)
```

## 6.9 Opportunity-type Candidate CTC Slab save

```mermaid
sequenceDiagram
    actor Sales
    participant Form as Opportunity Form
    participant Calc as ctcSlab.ts
    participant API as POST/PUT /api/opportunities
    participant ServerCalc as services/opportunity_ctc.py
    participant DB as opportunity_ctc_slabs

    Sales->>Form: Select T&M / Work Package / Fixed Price / Retainer
    Sales->>Form: Enter type-specific sources and slab Rate, Mgmt %, Hike %
    Form->>Calc: Recalculate type branch and full slab chain
    Calc-->>Form: Monthly, annual, engineering budget, approved CTC
    Sales->>API: Submit opportunity + ctc_slab
    API->>ServerCalc: Recalculate from authoritative details
    ServerCalc-->>API: Rounded derived values
    API->>DB: Replace ordered slab rows
    API-->>Sales: Saved opportunity
```
