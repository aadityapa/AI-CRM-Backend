# 1. System Architecture

High-level architecture of the KARNEX AI Interview + CRM platform.

## 1.1 High-level system architecture

```mermaid
graph TB
    subgraph clients["Clients (Browser)"]
        cand["Candidate UI<br/>(static index.html + js/)"]
        hrui["HR Interview UI<br/>(static index.html + js/)"]
        admin["React Admin Dashboard<br/>(Vite + TS, /admin)"]
    end

    subgraph server["FastAPI Backend (backend/main.py, uvicorn :2020)"]
        static["Static file serving<br/>frontend/ + admin-dashboard/dist"]
        legacy["Legacy Interview API<br/>@app.* routes in main.py<br/>auth: role-claim JWT (_require_user)"]
        crm["Karnex CRM API<br/>19 routers under /api/*<br/>auth: 7-role RBAC (role_required)"]
        adminr["Admin/Observability<br/>routers/admin.py (prompt logs, AI usage)"]

        subgraph svc["Service + Logic Layer"]
            aisvc["ai.py / services/interview<br/>question gen, eval, TTS, STT"]
            atssvc["ats.py<br/>resume ATS scoring"]
            crmsvc["services/* (CRM domain services)"]
            bridge["services/ai_interview_bridge.py<br/>(CRM ↔ Interview bridge)"]
            sess["session.py<br/>in-memory session state"]
        end

        subgraph data["Data Access"]
            authdb["auth_db.py<br/>raw SQL (interview tables)"]
            crmdb["crm_db.py + models/*<br/>SQLAlchemy 2.0 ORM"]
            alembic["Alembic migrations"]
        end
    end

    subgraph external["External Services"]
        openai["OpenAI API<br/>TTS · Whisper · Chat (question/eval)"]
        smtp["SMTP Server<br/>(invite emails, optional)"]
    end

    subgraph db["PostgreSQL (single database)"]
        pgcrm[("CRM tables<br/>~40 relational tables")]
        pglegacy[("Legacy interview tables<br/>registration_data, interview_*,<br/>job_templates, ai_prompt_logs")]
    end

    cand -->|HTTPS| static
    hrui -->|HTTPS| static
    admin -->|HTTPS| static
    cand -->|"/candidate/*, /next, /answer, /submit"| legacy
    hrui -->|"/auth/*, /hr/*, /job/*, /ats/*"| legacy
    admin -->|"/auth/*, /hr/*, /job/*"| legacy
    admin -->|"/api/* (CRM)"| crm
    admin -->|"/api/prompt-logs, /admin/ai/usage"| adminr

    legacy --> aisvc
    legacy --> atssvc
    legacy --> sess
    crm --> crmsvc
    crm --> bridge
    adminr --> authdb

    aisvc --> openai
    atssvc --> openai
    aisvc --> authdb
    bridge --> authdb
    bridge --> crmdb
    crmsvc --> crmdb
    legacy --> authdb
    legacy -->|invite emails| smtp

    authdb --> pglegacy
    crmdb --> pgcrm
    alembic -.->|schema migrations| pgcrm

    classDef ext fill:#fde68a,stroke:#b45309,color:#000
    classDef store fill:#bfdbfe,stroke:#1e40af,color:#000
    class openai,smtp ext
    class pgcrm,pglegacy store
```

## 1.2 Runtime / deployment topology

```mermaid
graph LR
    subgraph lan["LAN / Host machine"]
        browser["Browsers on network<br/>https://LAN-IP:2020"]
        subgraph proc["uvicorn process (:2020, HTTPS self-signed)"]
            app["FastAPI app (main:app)<br/>UVICORN_WORKERS=1"]
        end
        pg[("PostgreSQL<br/>localhost:5432 / karnex_db")]
    end

    subgraph cloud["Cloud (optional / prod)"]
        vercel["Vercel<br/>(static frontend)"]
        render["Render<br/>(backend, render.yaml)"]
        supa[("Supabase Postgres")]
        oai["OpenAI"]
        mail["Office365 SMTP"]
    end

    browser -->|"HTTPS + Bearer JWT"| app
    app --> pg
    app -.->|prod DSN| supa
    app --> oai
    app -.-> mail
    vercel -.->|prod| render
    render -.-> supa

    classDef opt stroke-dasharray: 5 5
    class cloud,vercel,render,supa,oai,mail opt
```

## 1.3 Authentication realms (one token, two authorizers)

```mermaid
graph TB
    login["POST /auth/login<br/>(main.py auth_login)"] -->|"HS256 JWT<br/>claims: sub, role, email, exp"| token(["Bearer token<br/>in localStorage"])

    token --> legacyauth["_require_user(request, allowed_roles)<br/>checks JWT 'role' claim<br/>hr / candidate / manager / admin"]
    token --> crmauth["get_current_user + role_required<br/>loads registration_data by sub,<br/>resolves roles from user_roles"]

    legacyauth --> legacyep["Legacy interview endpoints<br/>@app.* in main.py"]
    crmauth --> crmep["CRM endpoints<br/>/api/* (19 routers)"]

    note["Shared secret _auth_secret<br/>(AUTH_SECRET → REPORT_CODE → default,<br/>SHA-256 normalized if <32 bytes).<br/>Identical derivation in main.py and crm_deps.py."]
    token -.-> note

    classDef n fill:#fef9c3,stroke:#a16207,color:#000
    class note n
```

## 1.4 OpenAI purpose-keyed clients

```mermaid
graph LR
    subgraph clients["openai_client.py — process-wide clients by purpose"]
        transcribe["transcribe<br/>OPENAI_API_KEY_TRANSCRIBE"]
        tts["tts<br/>OPENAI_API_KEY_TTS"]
        question["question<br/>OPENAI_API_KEY_QUESTIONS"]
        evalc["eval<br/>OPENAI_API_KEY_EVALUATION"]
        default["default<br/>OPENAI_API_KEY"]
    end

    transcribe -->|"gpt-4o-mini-transcribe"| u1["POST /candidate/transcribe<br/>(Whisper STT)"]
    tts -->|"gpt-4o-mini-tts (nova)"| u2["POST /candidate/tts"]
    question --> u3["question generation +<br/>adaptive follow-ups"]
    evalc --> u4["per-turn scoring +<br/>full report evaluation"]
    default --> u5["ATS scoring / OCR / fallback"]

    note["Each purpose key falls back to OPENAI_API_KEY.<br/>All chat calls wrapped by prompt_logger for<br/>token logging + cost tracking (see /admin/ai/usage)."]
    clients -.-> note
    classDef n fill:#fef9c3,stroke:#a16207,color:#000
    class note n
```
