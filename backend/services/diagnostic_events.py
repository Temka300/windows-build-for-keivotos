"""Local structured diagnostics built on Python logging, independent of databases."""
from __future__ import annotations

from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import json
import logging
from logging.handlers import RotatingFileHandler
import math
from pathlib import Path
import re
import sys
import time
import traceback
from urllib.parse import quote, quote_plus
from uuid import uuid4

_CONTEXT = ContextVar("keivotos_diagnostic_context", default={})
_IDS = frozenset({"operation_owner", "operation_id", "attempt_id", "item_id", "library_id"})
_CODE = re.compile(r"[a-z][a-z0-9_]*(?:[.][a-z0-9_]+)*\Z")
_PRIVATE_KEY = re.compile(
    r"authorization|cookie|password|passphrase|apikey|token|credential|secret|"
    r"requestbody|responsebody|body|markdown|note|sentence|content", re.I)


def redact_log_text(value: str, secrets: tuple[str, ...] = ()) -> str:
    """Remove known credentials, URL queries and sensitive key/value text."""
    for secret in sorted({s for s in secrets if isinstance(s, str) and s}, key=len, reverse=True):
        for encoded in (secret, quote(secret, safe=""), quote_plus(secret)):
            value = value.replace(encoded, "[redacted]")
    value = re.sub(r"(https?://)[^\s/@]+:[^\s/@]+@", r"\1[redacted]@", value)
    value = re.sub(r"(https?://[^\s?#]+)[?#][^\s]*", r"\1?[redacted]", value)
    # HTTP access logs use a relative request target, not an http:// URL.
    value = re.sub(r"(\b(?:GET|HEAD|POST|PUT|PATCH|DELETE|OPTIONS)\s+)(/[^\s?#]*)[?#][^\s]*",
                   r"\1\2?[redacted]", value, flags=re.I)
    value = re.sub(
        r"""(?im)((?:proxy[-_])?authorization\s*["']?\s*[:=]\s*["']?)(?:basic|bearer)\s+[^\s"',;]+""",
        r"\1[redacted]", value)
    value = re.sub(r"""(?im)((?:set[-_])?cookie\s*["']?\s*[:=]\s*)[^\r\n]+""", r"\1[redacted]", value)
    return re.sub(
        r"""(?ix)(["']?(?:api[_-]?key|(?:access|refresh)[_-]?token|password|passphrase|secret|credential)["']?\s*[:=]\s*)
        (?:"[^"]*"|'[^']*'|[^\s&,;}]+)""", r"\1[redacted]", value)


def current_context() -> dict:
    return dict(_CONTEXT.get())


@contextmanager
def diagnostic_context(**identities):
    """Bind identities only to this task/thread; nesting restores the parent."""
    if set(identities) - _IDS:
        raise ValueError("Unknown diagnostic identity")
    if any(not isinstance(value, str) or not value or len(value) > 256 for value in identities.values()):
        raise ValueError("Diagnostic identities must be bounded nonempty strings")
    previous = _CONTEXT.set({**_CONTEXT.get(), **identities})
    try:
        yield
    finally:
        _CONTEXT.reset(previous)


class _Cleaner:
    def __init__(self, secrets):
        self.secrets = secrets
        self.remaining = 16384
        self.nodes = 128
        self.truncated = 0

    def text(self, value, limit=2048):
        value = redact_log_text(value, self.secrets).encode("utf-8", "replace").decode("utf-8")
        bound = min(limit, self.remaining)
        self.remaining -= min(len(value), bound)
        if len(value) > bound:
            self.truncated += 1
            return value[:bound] + "…[truncated]"
        return value

    def clean(self, value, depth=0):
        self.nodes -= 1
        if self.nodes < 0 or depth > 6:
            self.truncated += 1
            return "[truncated]"
        if value is None or isinstance(value, bool):
            return value
        if isinstance(value, int):
            if value.bit_length() < 256:
                return value
        elif isinstance(value, float):
            if math.isfinite(value):
                return value
        elif isinstance(value, str):
            return self.text(value)
        elif isinstance(value, Path):
            return self.text(str(value))
        elif isinstance(value, dict):
            result = {}
            for index, (key, item) in enumerate(value.items()):
                if index >= 32 or self.nodes <= 0:
                    self.truncated += 1
                    break
                if not isinstance(key, str):
                    self.truncated += 1
                    continue
                safe_key = self.text(key, 128)
                result[safe_key] = (
                    "[redacted]" if _PRIVATE_KEY.search(re.sub(r"[^a-z0-9]", "", key.lower()))
                    else self.clean(item, depth + 1))
            return result
        elif isinstance(value, (list, tuple)):
            result = []
            for item in value[:32]:
                if self.nodes <= 0:
                    break
                result.append(self.clean(item, depth + 1))
            if len(result) < len(value):
                self.truncated += 1
            return result
        self.truncated += 1
        return "[unsupported value]"


def _severity(record):
    level = record.levelno
    # Uvicorn logs all access responses at INFO; classify their actual outcome.
    if record.name == "uvicorn.access" and isinstance(record.args, tuple) and len(record.args) >= 5:
        try:
            status = int(record.args[4])
            level = max(level, logging.ERROR if status >= 500 else logging.WARNING if status >= 400 else logging.INFO)
        except (TypeError, ValueError):
            pass
    for threshold, text, number in (
        (50, "CRITICAL", 21), (40, "ERROR", 17), (30, "WARNING", 13),
        (20, "INFO", 9), (10, "DEBUG", 5)):
        if level >= threshold:
            return text, number
    return "TRACE", 1


class DiagnosticEventHandler(RotatingFileHandler):
    """One locked writer per process/run, using standard logging rotation.

    Repeat grouping retains bounded exception objects so Python object-ID reuse
    cannot accidentally identify a different failure as the same exception.
    Evidence is committed to that cache only after the line is written.
    """
    def __init__(self, path, run_id, *, max_bytes=5 * 1024 * 1024, backups=5,
                 exception_limit=256, exception_ttl=300, delay=False):
        self.run_id = run_id
        self._exceptions = OrderedDict()
        self._exception_limit = max(1, exception_limit)
        self._exception_ttl = max(0, exception_ttl)
        self._write_failed = False
        if not delay:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.write_guard = None
        super().__init__(path, maxBytes=max_bytes, backupCount=backups, encoding="utf-8", delay=delay)
        self.setFormatter(logging.Formatter("%(message)s"))

    def _event(self, record):
        secrets = getattr(record, "diagnostic_secrets", ())
        if not isinstance(secrets, (tuple, list)):
            secrets = ()
        cleaner = _Cleaner(secrets)
        try:
            message = record.getMessage()
        except Exception:
            message = "[unformattable log message]"
        severity, number = _severity(record)
        code = getattr(record, "event_code", "python.log")
        if not isinstance(code, str) or len(code) > 128 or not _CODE.fullmatch(code):
            code = "python.log"
        observed = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        try:
            timestamp = datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds")
        except (OverflowError, OSError, ValueError):
            timestamp = observed
        event = dict(
            schema_version=1, timestamp=timestamp, observed_timestamp=observed,
            severity_text=severity, severity_number=number,
            component=cleaner.text(record.name, 256), event_code=code,
            message=cleaner.text(message, 4096), run_id=self.run_id, event_id=uuid4().hex)
        context = current_context()
        for key in _IDS:
            value = getattr(record, key, context.get(key))
            if isinstance(value, str) and value:
                event[key] = cleaner.text(value, 256)
        details = getattr(record, "event_details", {})
        if not isinstance(details, dict):
            details = {}
            cleaner.truncated += 1
        event["details"] = cleaner.clean(details)
        pending = None
        now = time.monotonic()
        while self._exceptions and next(iter(self._exceptions.values()))["seen"] + self._exception_ttl <= now:
            self._exceptions.popitem(last=False)
        if record.exc_info and isinstance(record.exc_info[1], BaseException):
            exception = record.exc_info[1]
            prior = self._exceptions.get(id(exception))
            if prior is not None and prior["object"] is exception:
                event.update(exception_id=prior["id"], exception_event_id=prior["event"],
                             exception_repeat_count=prior["repeats"] + 1)
                pending = (exception, {**prior, "seen": now, "repeats": prior["repeats"] + 1})
            else:
                identity = uuid4().hex
                try:
                    description = str(exception)
                except Exception:
                    description = "[unprintable exception]"
                chunks, length = [], 0
                try:
                    for chunk in traceback.TracebackException(
                            record.exc_info[0], exception, record.exc_info[2],
                            limit=32, capture_locals=False, compact=True).format():
                        chunks.append(chunk[:12000 - length])
                        length += len(chunk)
                        if length >= 12000:
                            cleaner.truncated += 1
                            break
                except Exception:
                    chunks = ["[unavailable traceback]"]
                event.update(exception_id=identity, exception_event_id=event["event_id"],
                             exception={"type": type(exception).__name__,
                                        "message": cleaner.text(description),
                                        "stacktrace": cleaner.text("".join(chunks), 12000)})
                pending = (exception, dict(object=exception, id=identity, event=event["event_id"], seen=now, repeats=0))
        if cleaner.truncated:
            event["truncated_values"] = cleaner.truncated
        return event, pending

    def emit(self, record):
        # Handler.handle holds Python logging's RLock over serialization and write.
        try:
            event, pending = self._event(record)
            line = json.dumps(event, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
            prepared = logging.makeLogRecord({"msg": line, "args": (), "levelno": record.levelno})
            prepared._diagnostic_prepared = True
            self._write_failed = False
            if self.write_guard is not None and not self.write_guard(len(line.encode("utf-8"))+1):
                self.handleError(prepared)
            else:
                super().emit(prepared)
            if not self._write_failed and pending is not None:
                exception, entry = pending
                self._exceptions[id(exception)] = entry
                self._exceptions.move_to_end(id(exception))
                while len(self._exceptions) > self._exception_limit:
                    self._exceptions.popitem(last=False)
        except Exception:
            self.handleError(record)

    def handleError(self, record):
        # The stdlib default prints raw LogRecord arguments on failure.
        self._write_failed = True
        try:
            sys.stderr.write("Keivotos: a diagnostic event could not be written.\n")
        except Exception:
            pass

    def close(self):
        self.acquire()
        try:
            self._exceptions.clear()
            super().close()
        finally:
            self.release()
