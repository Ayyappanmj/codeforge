from __future__ import annotations

import logging
import re
import threading
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.exceptions import HTTPException as StarletteHTTPException

from codeforge_shared import metrics
from codeforge_shared.config import Settings
from codeforge_shared.languages import supported_languages
from codeforge_shared.logging_utils import configure_logging, log_event
from codeforge_shared.results import CancelOutcome
from codeforge_shared.states import ExecutionStatus
from codeforge_shared.store import SqlStore, make_session_factory

from .schemas import (
    CancelResponse, ExecutionCreate, ExecutionCreated, ExecutionDetail, ExecutionPage,
    ExecutionSummary,
)

log = logging.getLogger("codeforge.api")
REQUEST_ID_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")


def _error(status: int, code: str, message: str, details=None) -> JSONResponse:
    body = {"error": {"code": code, "message": message}}
    if details:
        body["error"]["details"] = details
    return JSONResponse(status_code=status, content=body)


class BodySizeLimitMiddleware:
    """Pure ASGI middleware: rejects oversized bodies, including chunked ones, before parsing."""

    def __init__(self, app, max_bytes: int):
        self.app, self.max_bytes = app, max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope["headers"])
        cl = headers.get(b"content-length")
        if cl and cl.isdigit() and int(cl) > self.max_bytes:
            return await _error(413, "payload_too_large", "Request body too large.")(scope, receive, send)
        received = 0

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise HTTPException(status_code=413, detail="Request body too large.")
            return message

        await self.app(scope, limited_receive, send)


def create_app(settings: Settings | None = None, store: SqlStore | None = None,
               rabbit_check=None) -> FastAPI:
    settings = settings or Settings.from_env()
    limits = settings.limits

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        configure_logging(settings.log_level)
        yield

    app = FastAPI(title="CodeForge API", version="1.0.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.store = store or SqlStore(make_session_factory(settings.database_url))
    # JSON escaping can inflate a payload up to ~6x, so the byte cap is generous but finite.
    app.add_middleware(BodySizeLimitMiddleware,
                       max_bytes=(limits.max_source_bytes + limits.max_stdin_bytes) * 6 + 8192)
    app.add_middleware(CORSMiddleware, allow_origins=list(settings.cors_origins),
                       allow_methods=["GET", "POST"],
                       allow_headers=["Content-Type", "X-Request-ID", "X-API-Key", "Authorization"])

    rate_limit_lock = threading.Lock()
    rate_limit_buckets: dict[str, list[float]] = {}

    @app.middleware("http")
    async def security_middleware(request: Request, call_next):
        path = request.url.path
        is_public = (
            path in ("/metrics", "/api/v1/health/live", "/api/v1/health/ready")
            or path.startswith(("/docs", "/openapi.json", "/redoc"))
        )
        if not is_public and settings.api_key:
            auth_header = request.headers.get("x-api-key", "")
            if not auth_header and "authorization" in request.headers:
                auth_val = request.headers.get("authorization", "")
                if auth_val.startswith("Bearer "):
                    auth_header = auth_val[7:].strip()
            if auth_header != settings.api_key:
                return _error(401, "unauthorized", "Invalid or missing API key.")

        if not is_public and settings.rate_limit_per_minute > 0:
            client_ip = request.client.host if request.client else "unknown"
            forwarded = request.headers.get("x-forwarded-for")
            if forwarded:
                client_ip = forwarded.split(",")[0].strip()
            now = time.monotonic()
            with rate_limit_lock:
                timestamps = [t for t in rate_limit_buckets.get(client_ip, []) if now - t < 60.0]
                if len(timestamps) >= settings.rate_limit_per_minute:
                    return _error(429, "rate_limit_exceeded", "Rate limit exceeded. Please wait before retrying.")
                timestamps.append(now)
                rate_limit_buckets[client_ip] = timestamps

        return await call_next(request)

    def check_rabbit() -> bool:
        if rabbit_check is not None:
            return rabbit_check()
        try:
            from codeforge_shared import mq
            conn = mq.connect(settings.rabbitmq_url)
            conn.close()
            return True
        except Exception:  # noqa: BLE001
            return False

    @app.middleware("http")
    async def request_id(request: Request, call_next):
        rid = request.headers.get("x-request-id", "")
        rid = rid if REQUEST_ID_RE.fullmatch(rid) else uuid.uuid4().hex
        request.state.request_id = rid
        response = await call_next(request)
        response.headers["X-Request-ID"] = rid
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_handler(_request, exc: RequestValidationError):
        details = [{"field": ".".join(str(p) for p in e["loc"][1:]), "message": e["msg"]}
                   for e in exc.errors()]  # never echo submitted input
        return _error(422, "validation_error", "Invalid request.", details)

    @app.exception_handler(StarletteHTTPException)
    async def http_handler(_request, exc: StarletteHTTPException):
        code = {404: "not_found", 409: "conflict", 413: "payload_too_large"}.get(
            exc.status_code, "http_error")
        return _error(exc.status_code, code, str(exc.detail))

    @app.exception_handler(Exception)
    async def unhandled(request, exc: Exception):
        log_event(log, logging.ERROR, "unhandled_exception", exc_info=True,
                  path=request.url.path)
        return _error(500, "internal_error", "Internal server error.")

    def parse_id(execution_id: str) -> uuid.UUID:
        try:
            return uuid.UUID(execution_id)
        except ValueError:
            raise HTTPException(status_code=404, detail="Execution not found.") from None

    v1 = "/api/v1"

    @app.post(f"{v1}/executions", status_code=202, response_model=ExecutionCreated)
    def create_execution(body: ExecutionCreate, request: Request):
        if len(body.source_code.encode()) > limits.max_source_bytes:
            raise HTTPException(413, f"source_code exceeds {limits.max_source_bytes} bytes.")
        if len(body.stdin.encode()) > limits.max_stdin_bytes:
            raise HTTPException(413, f"stdin exceeds {limits.max_stdin_bytes} bytes.")
        if "\x00" in body.source_code or "\x00" in body.stdin:
            raise HTTPException(422, "NUL characters are not allowed.")
        ex = app.state.store.create_execution(
            language=body.language, source_code=body.source_code, stdin=body.stdin,
            correlation_id=request.state.request_id)
        metrics.SUBMISSIONS.labels(body.language).inc()
        log_event(log, logging.INFO, "execution_submitted", execution_id=str(ex.id),
                  correlation_id=request.state.request_id, language=body.language)
        return ExecutionCreated(execution_id=ex.id, status=ex.status)

    @app.get(f"{v1}/executions", response_model=ExecutionPage)
    def list_executions(page: int = Query(1, ge=1, le=100_000), page_size: int = Query(20, ge=1, le=100),
                        status: ExecutionStatus | None = None, language: str | None = Query(None, max_length=32)):
        if language is not None and language not in supported_languages():
            raise HTTPException(422, "Unsupported language filter.")
        rows, total = app.state.store.list_executions(
            page=page, page_size=page_size, status=status.value if status else None, language=language)
        return ExecutionPage(items=[ExecutionSummary.model_validate(r) for r in rows],
                             total=total, page=page, page_size=page_size)

    @app.get(f"{v1}/languages")
    def languages():
        return {"languages": supported_languages()}

    @app.get(f"{v1}/executions/{{execution_id}}", response_model=ExecutionDetail)
    def get_execution(execution_id: str):
        ex = app.state.store.get_execution(parse_id(execution_id))
        if ex is None:
            raise HTTPException(404, "Execution not found.")
        return ExecutionDetail.model_validate(ex)

    @app.post(f"{v1}/executions/{{execution_id}}/cancel", response_model=CancelResponse)
    def cancel_execution(execution_id: str, response: Response):
        eid = parse_id(execution_id)
        outcome = app.state.store.request_cancel(eid)
        if outcome is CancelOutcome.NOT_FOUND:
            raise HTTPException(404, "Execution not found.")
        if outcome is CancelOutcome.NOT_CANCELLABLE:
            raise HTTPException(409, "Execution already finished and cannot be cancelled.")
        ex = app.state.store.get_execution(eid)
        response.status_code = 200 if outcome is CancelOutcome.CANCELLED else 202
        return CancelResponse(execution_id=eid, status=ex.status, cancel_requested=True)

    @app.get(f"{v1}/health/live")
    def live():
        return {"status": "ok"}

    @app.get(f"{v1}/health/ready")
    def ready():
        checks = {"postgres": "ok", "rabbitmq": "ok"}
        try:
            app.state.store.ping()
        except Exception:  # noqa: BLE001
            checks["postgres"] = "unavailable"
        if not check_rabbit():
            # Not fatal: the transactional outbox buffers submissions while RabbitMQ is down.
            checks["rabbitmq"] = "unavailable (submissions are buffered in the outbox)"
        ok = checks["postgres"] == "ok"
        return JSONResponse(status_code=200 if ok else 503,
                            content={"status": "ready" if ok else "not_ready", "checks": checks})

    @app.get("/metrics")
    def prometheus_metrics():
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return app
