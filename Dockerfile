# Backend-only image (split repo — UI lives in AI-Interview-Model-F-V2).
# Render sets PORT at runtime (default 10000).

FROM python:3.12-slim AS python-deps

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY backend/requirements.txt /app/backend/requirements.txt
RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir --prefix=/install -r /app/backend/requirements.txt

FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
ENV PORT=10000
ENV UVICORN_WORKERS=1
ENV PROMPT_LOG_ENABLED=true
ENV PROMPT_LOG_FILE_ENABLED=false

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=python-deps /install /usr/local
COPY backend /app/backend

RUN mkdir -p /app/data /app/logs

WORKDIR /app/backend

EXPOSE 10000

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT}/health/live" >/dev/null || exit 1

CMD sh -c "python -m uvicorn main:app --host 0.0.0.0 --port ${PORT} --workers ${UVICORN_WORKERS}"
