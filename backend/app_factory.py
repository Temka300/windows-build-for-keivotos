"""The FastAPI application object and its HTTP middleware.

Moved verbatim from ``core.py``, which no longer constructs the application.
``server.py`` imports ``app`` from here and attaches the routers; ``core`` keeps
re-exporting it while unconverted routers still wildcard-import the facade.

The browser-access middleware is a security control: it rejects any request
whose Host, Origin, or Fetch Metadata does not match the active local origin.
No ``core`` import.
"""
from __future__ import annotations

import logging
import os
import time

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.exception_handlers import http_exception_handler
from starlette.exceptions import HTTPException

from lifecycle import lifespan
from product import DISPLAY_NAME, VERSION
from security import validate_local_browser_request


app = FastAPI(title=DISPLAY_NAME, version=VERSION, lifespan=lifespan)


@app.middleware("http")
async def restrict_local_browser_access(request: Request, call_next):
    rejection = validate_local_browser_request(
        host_header=request.headers.get("host", ""),
        scheme=request.url.scheme,
        origin_header=request.headers.get("origin"),
        fetch_site_header=request.headers.get("sec-fetch-site"),
        lan_host=os.environ.get("KEIVOTOS_LAN_HOST"),
    )
    if rejection is not None:
        status_code, detail = rejection
        return JSONResponse(status_code=status_code, content={"detail": detail})
    return await call_next(request)


@app.middleware("http")
async def add_server_timing_header(request: Request, call_next):
    started_at = time.perf_counter()
    response = await call_next(request)
    duration_ms = (time.perf_counter() - started_at) * 1000
    response.headers["Server-Timing"] = f'app;dur={duration_ms:.2f};desc="{DISPLAY_NAME}"'
    return response


@app.exception_handler(HTTPException)
async def log_http_error(request: Request, exception):
    level = logging.ERROR if exception.status_code >= 500 else logging.WARNING
    logging.getLogger("keivotos.requests").log(
        level, "%s %s: HTTP %s", request.method, request.url.path, exception.status_code,
        extra={"event_code": "http.request_rejected",
               "event_details": {"method": request.method, "status_code": exception.status_code}})
    return await http_exception_handler(request, exception)


class _DiagnosticRequestContext:
    """Keep correlation through streamed response sending and Uvicorn access logs."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        from uuid import UUID, uuid4
        from services.diagnostic_events import diagnostic_context
        from services.operation_diagnostics import emit
        supplied = next((value for key, value in scope.get("headers", ())
                         if key.lower() == b"x-keivotos-operation-id"), b"")
        try:
            identity = str(UUID(supplied.decode("ascii"))) if len(supplied) <= 36 else uuid4().hex
        except (ValueError, UnicodeError):
            identity = uuid4().hex
        with diagnostic_context(operation_owner="suite", operation_id=identity):
            async def correlated_send(message):
                if message["type"] == "http.response.start":
                    message = dict(message)
                    headers = [(key, value) for key, value in message.get("headers", ())
                               if key.lower() != b"x-keivotos-operation-id"]
                    headers.append((b"x-keivotos-operation-id", identity.encode("ascii")))
                    message["headers"] = headers
                await send(message)
            try:
                await self.app(scope, receive, correlated_send)
            except Exception as error:
                emit("requests", "http.request_failed", "HTTP request failed",
                     level=logging.ERROR, details={"method": scope.get("method", "")}, exception=error)
                raise


app.add_middleware(_DiagnosticRequestContext)
