"""Operation evidence, not operational state: no profile or database initialization."""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from hashlib import sha256
import inspect
import logging
import time
from uuid import uuid4

from services.diagnostic_events import current_context, diagnostic_context

_CURRENT = ContextVar("keivotos_diagnostic_phase", default=None)
_COUNT_KEYS = ("processed", "success", "skipped", "failed")


def emit(owner, code, message, *, level=logging.INFO, details=None, exception=None, secrets=()):
    """Logging must never change an owner's result or cancellation behavior."""
    try:
        logging.getLogger("keivotos." + owner).log(
            level, message, extra={"event_code": code, "event_details": details or {},
                                   "diagnostic_secrets": secrets},
            exc_info=(type(exception), exception, exception.__traceback__) if exception else None)
    except Exception:
        pass


def _identities(owner, identity=None, library=None, attempt=None):
    context = current_context()
    values = {"operation_owner": context.get("operation_owner", owner),
              "operation_id": context.get("operation_id", uuid4().hex)}
    if isinstance(identity, str) and 0 < len(identity) <= 256:
        values.update(operation_owner=owner, operation_id=identity)
    if isinstance(library, str) and 0 < len(library) <= 256:
        values["library_id"] = library
    if isinstance(attempt, str) and attempt:
        values["attempt_id"] = sha256(attempt.encode()).hexdigest()[:32]
    return values


class Phase:
    def __init__(self, owner, action):
        self.owner, self.action = owner, action
        self.id = uuid4().hex
        self.counts = dict.fromkeys(_COUNT_KEYS, 0)
        self.reasons = {}
        self.outcome = None

    def count(self, name, amount=1, *, reason=None):
        if name not in self.counts or type(amount) is not int or amount < 0:
            raise ValueError("Invalid diagnostic count")
        self.counts[name] += amount
        if reason:
            # Reasons are fixed owner codes, never arbitrary exception/body text.
            key = str(reason)[:80]
            if key in self.reasons or len(self.reasons) < 16:
                self.reasons[key] = self.reasons.get(key, 0) + amount

    def result(self, value):
        if not isinstance(value, dict):
            return
        for name in _COUNT_KEYS:
            number = value.get(name)
            if type(number) is int and number >= 0:
                self.counts[name] += number
        if not any(type(value.get(key)) is int for key in _COUNT_KEYS):
            total = sum(value[key] for key in ("files", "directories")
                        if type(value.get(key)) is int and value[key] >= 0)
            if total:
                self.counts["success"] += total
                self.counts["processed"] += total + self.counts["skipped"] + self.counts["failed"]


@contextmanager
def operation(owner, action, *, identity=None, library=None, attempt=None):
    parent = _CURRENT.get()
    phase = Phase(owner, action)
    started = time.monotonic()
    with diagnostic_context(**_identities(owner, identity, library, attempt)):
        token = _CURRENT.set(phase)
        base = {"phase": action, "phase_id": phase.id}
        if parent:
            base["parent_phase_id"] = parent.id
        emit(owner, "operation.phase_started", action + " started", details=base)
        exception = None
        try:
            yield phase
        except BaseException as error:
            exception = error
            name = type(error).__name__
            phase.outcome = "cancelled" if isinstance(error, (InterruptedError, KeyboardInterrupt)) or name == "CancelledError" else "failed"
            if phase.outcome == "failed":
                phase.counts["failed"] = max(1, phase.counts["failed"])
            raise
        finally:
            try:
                outcome = phase.outcome or ("completed-with-warnings" if phase.counts["failed"] else "completed")
                detail = {**base, "outcome": outcome, "duration_ms": round(max(0, time.monotonic()-started)*1000, 3),
                          **phase.counts, "reasons": phase.reasons}
                expected = exception is not None and (isinstance(exception, ValueError) or outcome=="cancelled" or type(exception).__name__=="ServeDenied")
                if exception is not None:
                    detail["error_type"] = type(exception).__name__
                emit(owner, "operation.phase_finished", action + ": " + outcome,
                     level=logging.WARNING if expected else logging.ERROR if outcome == "failed" else logging.WARNING if outcome == "interrupted" else logging.INFO,
                     details=detail, exception=None if expected else exception)
            finally:
                _CURRENT.reset(token)


def count(name, amount=1, *, reason=None):
    phase = _CURRENT.get()
    if phase is not None:
        phase.count(name, amount, reason=reason)


def observed(owner, action, *, identity=None, library=None, false_outcome=None, exit_result=False):
    """Wrap a real boundary; arguments/body values are never serialized."""
    def decorate(function):
        signature = inspect.signature(function)
        def identities(args, kwargs):
            if identity is None and library is None:
                return {}
            try:
                bound = signature.bind(*args, **kwargs).arguments
                def select(key):
                    if not key:
                        return None
                    parts = key.split(".")
                    value = bound.get(parts[0])
                    for part in parts[1:]:
                        value = value.get(part) if isinstance(value, dict) else None
                    return value
                return dict(identity=select(identity), library=select(library))
            except (TypeError, ValueError):
                return {}
        if inspect.iscoroutinefunction(function):
            @wraps(function)
            async def asynchronous(*args, **kwargs):
                with operation(owner, action, **identities(args, kwargs)) as phase:
                    result = await function(*args, **kwargs)
                    phase.result(result)
                    if result is False and false_outcome:
                        phase.outcome = false_outcome
                    if exit_result and type(result) is int and result != 0:
                        phase.outcome = "failed"
                        phase.count("failed")
                    return result
            return asynchronous
        @wraps(function)
        def synchronous(*args, **kwargs):
            with operation(owner, action, **identities(args, kwargs)) as phase:
                result = function(*args, **kwargs)
                phase.result(result)
                if result is False and false_outcome:
                    phase.outcome = false_outcome
                if exit_result and type(result) is int and result != 0:
                    phase.outcome = "failed"
                    phase.count("failed")
                return result
        return synchronous
    return decorate


def snapshot(connection, owner, identity):
    """Read the already selected jobs connection; callers publish after commit."""
    cursor = connection.execute(
        "SELECT o.state,o.phase,o.attempt,a.token FROM kv_operations o "
        "LEFT JOIN kv_job_attempts a ON a.owner=o.owner AND a.operation_id=o.id AND a.number=o.attempt "
        "WHERE o.owner=? AND o.id=?", (owner, identity))
    row = cursor.fetchone()
    if row is None:
        return None
    return dict(zip(("state", "phase", "attempt", "attempt_token"), tuple(row.values()) if isinstance(row, dict) else tuple(row)))


def committed(owner, identity, state, *, reason=None, previous=None, counts=None):
    if state is None:
        return
    with diagnostic_context(**_identities(owner, identity, attempt=state.get("attempt_token"))):
        details = {key: state[key] for key in ("state", "phase", "attempt") if key in state}
        if reason:
            details["reason"] = reason
        if previous:
            details["previous_state"] = previous
        if counts is None and state["state"] in ("completed","completed-with-warnings","failed","cancelled","interrupted"):
            counts={"processed":1 if state["state"] in ("completed","completed-with-warnings","failed") else 0,
                    "success":1 if state["state"] in ("completed","completed-with-warnings") else 0,
                    "skipped":0,"failed":1 if state["state"]=="failed" else 0}
        if counts:
            details["counts"] = {key: value for key, value in counts.items()
                                 if isinstance(key, str) and type(value) is int and value >= 0}
        emit(owner, "operation.state_committed", "Operation " + state["state"],
             level=logging.ERROR if state["state"] == "failed" else logging.WARNING if state["state"] == "interrupted" else logging.INFO,
             details=details)


def failure(owner, code, *, reason, details=None, exception=None):
    emit(owner, code, reason, level=logging.WARNING, details=details, exception=exception)


def worker_environment(environment, *, profile=None):
    import json, os
    result = dict(environment)
    selected_profile = str(profile) if profile is not None else result.get("KEIVOTOS_HOME") or os.environ.get("KEIVOTOS_HOME")
    if selected_profile:
        result["KEIVOTOS_HOME"] = selected_profile
    selected = current_context()
    if selected:
        result["KEIVOTOS_DIAGNOSTIC_CONTEXT"] = json.dumps(selected)
    return result


def worker_diagnostics(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        import os, json
        value = os.environ.get("KEIVOTOS_DIAGNOSTIC_CONTEXT")
        if not value or not os.environ.get("KEIVOTOS_HOME"):
            return function(*args, **kwargs)
        identities = json.loads(value)
        with diagnostic_context(**identities):
            from runtime_logging import configure_runtime_logging
            configured=False
            try:
                configure_runtime_logging()
                configured=True
            except OSError:
                import sys
                try:sys.stderr.write("Keivotos: worker diagnostic storage is unavailable.\n")
                except Exception:pass
            try:
                return function(*args, **kwargs)
            finally:
                if configured:logging.shutdown()
    return wrapped


def outcome(value):
    if value not in ("completed","completed-with-warnings","failed","cancelled","interrupted","skipped","deferred"):
        raise ValueError("Invalid diagnostic outcome")
    phase = _CURRENT.get()
    if phase is not None:
        phase.outcome = value
