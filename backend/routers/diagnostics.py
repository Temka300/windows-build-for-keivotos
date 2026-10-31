"""Restricted local browser diagnostic intake; no log browsing or raw error bodies."""
from collections import deque
import threading
import logging
import time
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from services.diagnostic_events import diagnostic_context
from services.operation_diagnostics import emit

router = APIRouter()
_recent = deque()
_lock = threading.Lock()


class BrowserDiagnostic(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    code: Literal["script_error", "unhandled_rejection", "request_failed", "media_failed", "reader_failed"]
    component: Literal["suite", "files", "danbooru", "youtube", "twitter", "video", "manga", "ost", "language", "unknown"] = "suite"
    operation_id: str | None = Field(default=None, pattern=r"^(?:[0-9a-f]{32}|[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})$")
    error_type: Literal["Error", "TypeError", "RangeError", "ReferenceError", "SyntaxError", "URIError", "DOMException", "NetworkError"] = "Error"
    status: int | None = Field(default=None, ge=0, le=599)
    line: int | None = Field(default=None, ge=0, le=10000000)
    column: int | None = Field(default=None, ge=0, le=10000000)
    reason: Literal["network", "decode", "unsupported_format", "unsupported_codec", "playback", "fullscreen", "reader"] | None = None


@router.post("/api/diagnostics/frontend")
def record_browser_diagnostic(event: BrowserDiagnostic):
    now = time.monotonic()
    with _lock:
        while _recent and _recent[0] < now - 60:
            _recent.popleft()
        if len(_recent) >= 60:
            raise HTTPException(429, "Diagnostic intake limit reached")
        _recent.append(now)
    identity = event.operation_id or uuid4().hex
    with diagnostic_context(operation_owner=event.component, operation_id=identity):
        emit("frontend", "frontend." + event.code, "Browser " + event.code.replace("_", " "),
             level=logging.ERROR if event.code in ("script_error","unhandled_rejection") or (event.status or 0)>=500 else logging.WARNING,
             details=event.model_dump(exclude={"operation_id"}, exclude_none=True))
    return {"status": "recorded", "operation_id": identity}


class LogLimits(BaseModel):
    model_config = ConfigDict(extra="forbid",strict=True)
    retention_days: int = Field(default=14,ge=1,le=365)
    total_mib: int = Field(default=256,ge=16,le=4096)
    rotation_mib: int = Field(default=16,ge=1,le=64)


class DebugRequest(BaseModel):
    model_config = ConfigDict(extra="forbid",strict=True)
    active: bool


@router.get("/api/diagnostics/policy")
def log_policy():
    import config
    from runtime_logging import diagnostics_state,debug_status
    return {"limits":config.get_log_policy(),"debug":debug_status(),"status":diagnostics_state()}


@router.put("/api/diagnostics/policy")
def update_log_policy(values:LogLimits):
    import config
    from runtime_logging import apply_log_policy
    from services.diagnostic_policy import policy
    try:
        limits=policy(values.model_dump())
    except ValueError as error:
        raise HTTPException(422,str(error)) from error
    config.save_config({"diagnostic_limits":limits})
    apply_log_policy()
    emit("diagnostics","diagnostics.policy_changed","Diagnostic limits updated",details=limits)
    return log_policy()


@router.post("/api/diagnostics/debug")
def change_debug_session(request:DebugRequest):
    from runtime_logging import set_debug_session
    set_debug_session(request.active)
    return log_policy()
