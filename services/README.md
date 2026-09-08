# Karnex Microservices

Incremental extraction from `backend/main.py` using the **strangler fig** pattern.

## Phase 1 (deployable today)

| Service | Port | Routes |
|---------|------|--------|
| [api-gateway](api-gateway/) | 8080 | All traffic entry |
| [auth-service](auth-service/) | 8001 | `/auth/*` |
| [candidate-service](candidate-service/) | 8002 | `/hr/candidates*`, `/candidates/ranked` |
| [template-service](template-service/) | 8003 | `/job/*`, `/masters/*` |

Run: `docker compose -f docker-compose.yml -f docker-compose.microservices.yml up -d --build`

## Phase 2+ (planned — not in repo yet)

- `interview-service` — `/setup`, `/next`, `/answer`, `/submit`
- `ai-question-service` — OpenAI question generation
- `speech-service` — STT/TTS
- `evaluation-service` — AI scoring
- `report-service` — reports + PDF
- `ats-service` — resume/JD matching
- `integrity-service` — proctoring events
- `notification-service` — email/SMS
- `analytics-service` — HR dashboard aggregates
