"""Bounded, observable retries for idempotent Danbooru JSON reads."""
from __future__ import annotations

import http.client
import json
import ssl
import time
import logging
from uuid import uuid4
from services.diagnostic_events import current_context, diagnostic_context
from services.operation_diagnostics import emit as diagnostic_event, observed, count, outcome as phase_outcome
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Callable, Any

from runtime_logging import redact_log_text


def failure_kind(error: Exception) -> str:
    if isinstance(error, urllib.error.HTTPError):
        if error.code == 401:
            return "authentication"
        if error.code == 403:
            challenged = any(name.lower() == "cf-mitigated" and value.strip().lower() == "challenge"
                             for name, value in (error.headers or {}).items())
            return "site challenge" if challenged else "access denied"
        if error.code == 429:
            return "rate limit"
        return "server" if error.code in (500, 502, 503, 504) else "request"
    reason = error.reason if isinstance(error, urllib.error.URLError) else error
    if isinstance(reason, ssl.SSLCertVerificationError):
        return "certificate"
    if isinstance(reason, (TimeoutError, ConnectionError, http.client.HTTPException)):
        return "connection"
    if isinstance(error, urllib.error.URLError):
        return "connection"
    return "invalid response" if isinstance(error, (ValueError, UnicodeError)) else "local"


def http_failure_detail(error: urllib.error.HTTPError) -> str:
    """Explain HTTP failures without retaining response HTML or blaming every 403 on a key."""
    kind = failure_kind(error)
    if kind == "authentication":
        return "Danbooru rejected the credentials (HTTP 401)."
    if kind == "site challenge":
        return "Danbooru blocked this request with a browser challenge (HTTP 403)."
    if kind == "access denied":
        return "Danbooru denied access (HTTP 403)."
    if kind == "rate limit":
        return "Danbooru rate limited this request (HTTP 429); retry later."
    return f"Danbooru request failed (HTTP {error.code})."


def retry_delay(error: Exception, attempt: int) -> float | None:
    kind = failure_kind(error)
    if kind not in {"connection", "rate limit", "server"}:
        return None
    delay = min(2 ** (attempt + 1), 30)
    if kind == "rate limit":
        delay = min(60 * (attempt + 1), 300)
    if isinstance(error, urllib.error.HTTPError) and error.headers:
        value = error.headers.get("Retry-After")
        if value:
            try:
                seconds = float(value)
            except ValueError:
                try:
                    target = parsedate_to_datetime(value)
                    if target.tzinfo is None:
                        target = target.replace(tzinfo=timezone.utc)
                    seconds = (target - datetime.now(timezone.utc)).total_seconds()
                except (ValueError, TypeError, OverflowError):
                    seconds = 0
            # Never retry earlier than requested. Long waits require a later run.
            if seconds > 300:
                return None
            if seconds > 0:
                delay = max(delay, seconds)
    return delay


@observed("danbooru", "network.read")
def read_json(request: urllib.request.Request, *, timeout: float, retries: int,
              emit: Callable[[str], None], secrets: tuple[str, ...] = (),
              not_found_empty: bool = False) -> Any:
    retries = max(0, min(retries, 5))
    endpoint = urllib.parse.urlsplit(request.full_url).path
    for attempt in range(retries + 1):
        count("processed")
        attempt_id = uuid4().hex
        started = time.monotonic()
        parent_attempt = current_context().get("attempt_id")
        def record_attempt(outcome, **extra):
            count("success" if outcome=="completed" else "skipped" if outcome=="skipped" else "failed")
            if outcome in ("completed","skipped"):phase_outcome(outcome)
            with diagnostic_context(attempt_id=attempt_id):
                diagnostic_event("danbooru", "network.attempt_finished", "Network attempt " + outcome,
                    level=logging.WARNING if outcome != "completed" else logging.INFO,
                    details={"outcome":outcome,"endpoint":endpoint,"attempt_number":attempt+1,
                             "duration_ms":round((time.monotonic()-started)*1000,3),
                             "parent_attempt_id":parent_attempt,**extra}, secrets=secrets)
        context = f"GET {endpoint} (attempt {attempt + 1}/{retries + 1})"
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                result = json.loads(response.read().decode("utf-8"))
                record_attempt("completed")
                emit(f"Request: {context} succeeded")
                return result
        except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError) as error:
            if isinstance(error, urllib.error.HTTPError) and error.code == 404 and not_found_empty:
                record_attempt("skipped",reason="not_found")
                emit(f"Request: {context}: HTTP 404, no matching post")
                return []
            kind = failure_kind(error)
            description = f"HTTP {error.code}: {error.reason}" if isinstance(error, urllib.error.HTTPError) else str(error)
            detail = redact_log_text(description, secrets)
            delay = retry_delay(error, attempt)
            if attempt < retries and delay is not None:
                emit(f"WARNING: {context}: {kind}: {detail}; retry in {delay:g}s")
                record_attempt("failed",reason=kind,retry=True,retry_delay_seconds=delay)
                time.sleep(delay)
                continue
            record_attempt("failed",reason=kind,retry=False)
            ending = "retries exhausted" if delay is not None else "request stopped"
            emit(f"ERROR: {context}: {kind}: {detail}; {ending}")
            raise
