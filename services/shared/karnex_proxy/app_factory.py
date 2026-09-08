"""
Strangler-proxy FastAPI app — forwards requests to the monolith unchanged.

Phase 1: independent containers + routing without duplicating business logic.
Phase 2+: replace proxy handlers with extracted domain code per service.
"""
from __future__ import annotations

import os
from typing import Iterable

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse


def create_proxy_app(
    *,
    title: str,
    path_prefixes: Iterable[str],
    upstream: str | None = None,
    service_id: str | None = None,
) -> FastAPI:
    upstream_base = (upstream or os.getenv("MONOLITH_URL", "http://127.0.0.1:8010")).rstrip("/")
    prefixes = tuple(path_prefixes)
    sid = service_id or title

    app = FastAPI(
        title=title,
        version=os.getenv("SERVICE_VERSION", "1.0.0"),
        description="Karnex microservice (strangler proxy phase). Business logic remains in monolith until extraction.",
    )

    def _allowed(path: str) -> bool:
        if not path.startswith("/"):
            path = f"/{path}"
        for p in prefixes:
            base = p if p.startswith("/") else f"/{p}"
            if path == base or path.startswith(f"{base}/"):
                return True
        return False

    @app.get("/health/live")
    async def health_live():
        return {"status": "ok", "service": sid, "mode": "strangler-proxy"}

    @app.get("/health/ready")
    async def health_ready():
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.get(f"{upstream_base}/readyz")
                if r.status_code < 500:
                    return {"status": "ok", "service": sid, "upstream": upstream_base}
        except Exception as exc:
            return JSONResponse(
                status_code=503,
                content={"status": "degraded", "service": sid, "error": str(exc)},
            )
        return JSONResponse(status_code=503, content={"status": "degraded", "service": sid})

    @app.api_route(
        "/{full_path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
    )
    async def proxy_request(request: Request, full_path: str) -> Response:
        path = f"/{full_path}" if full_path else "/"
        if not _allowed(path):
            return JSONResponse(
                status_code=404,
                content={"error": f"Path not served by {sid}", "path": path},
            )

        url = f"{upstream_base}{path}"
        if request.url.query:
            url = f"{url}?{request.url.query}"

        headers = dict(request.headers)
        headers.pop("host", None)
        headers["X-Karnex-Service"] = sid
        trace = request.headers.get("x-request-id") or request.headers.get("x-correlation-id")
        if trace:
            headers["X-Correlation-Id"] = trace

        body = await request.body()
        timeout = float(os.getenv("PROXY_TIMEOUT_S", "120"))

        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
                upstream_resp = await client.request(
                    request.method,
                    url,
                    headers=headers,
                    content=body if body else None,
                )
        except httpx.RequestError as exc:
            return JSONResponse(
                status_code=502,
                content={"error": "upstream unavailable", "service": sid, "detail": str(exc)},
            )

        resp_headers = {
            k: v
            for k, v in upstream_resp.headers.items()
            if k.lower() not in {"transfer-encoding", "connection", "content-encoding", "content-length"}
        }
        return Response(
            content=upstream_resp.content,
            status_code=upstream_resp.status_code,
            headers=resp_headers,
            media_type=upstream_resp.headers.get("content-type"),
        )

    return app
