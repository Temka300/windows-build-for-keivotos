"""Persistent, dated application and HTTP access logging."""

from __future__ import annotations

import logging
import json
from collections import deque
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

from config import (
    get_diagnostics_preferences,
    get_log_policy,
    DIAGNOSTIC_LOG_FILE,
    DIAGNOSTIC_RUN_ID,
    ACCESS_LOG_FILE,
    LOG_DIR,
    LOG_FILE_LIMIT_MB,
    LOG_RETENTION_FILES,
    LOG_ROLLOVER_FILES,
    RUNTIME_LOG_FILE,
    SUITE_LOG_PREFIX,
)

from services.diagnostic_events import DiagnosticEventHandler, redact_log_text, current_context
from services.diagnostic_policy import LogSession, MIB, guarded, debug_session
from services.log_bootstrap import fallback_root, notice as bootstrap_notice, state as bootstrap_state


_logging_lock = threading.RLock()
_owned_handlers: list[logging.Handler] = []


class _SafeFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        secrets = getattr(record, "diagnostic_secrets", ())
        value = super().format(record)
        details=getattr(record,"event_details",{})
        if isinstance(details,dict):
            facts=[]
            if getattr(record,"event_code","") in ("operation.phase_finished","operation.state_committed","lifecycle.ready"):
                counts=details.get("counts",details)
                for key in ("duration_ms","processed","success","skipped","failed","libraries"):
                    number=counts.get(key) if isinstance(counts,dict) else None
                    if type(number) in (int,float):facts.append(key+"="+str(number))
                    elif key=="libraries" and key in counts:facts.append("libraries=unavailable")
                modules=details.get("modules")
                if isinstance(modules,list):
                    facts.extend(str(item.get("module"))+":"+str(item.get("state",item.get("status","available"))) for item in modules if isinstance(item,dict))
            if facts:value+=" ["+" ".join(facts)+"]"
        context = current_context()
        identifiers = []
        for key,label in (("operation_id","operation"),("attempt_id","attempt")):
            identity = getattr(record,key,context.get(key))
            if isinstance(identity,str) and identity:
                identifiers.append(label+"="+identity[:256])
        if identifiers:
            value += " ["+" ".join(identifiers)+"]"
        return redact_log_text(value, secrets if isinstance(secrets, (tuple, list)) else ())


def _unhandled_exception(kind, value, traceback) -> None:
    if issubclass(kind, KeyboardInterrupt):
        sys.__excepthook__(kind, value, traceback)
        return
    logging.getLogger("keivotos.crash").critical(
        "Unhandled application error", exc_info=(kind, value, traceback),
        extra={"event_code": "runtime.unhandled_exception"})


def _unhandled_thread_exception(arguments) -> None:
    if arguments.exc_type is SystemExit:
        return
    logging.getLogger("keivotos.crash").critical(
        "Unhandled background error in %s", arguments.thread.name if arguments.thread else "unknown thread",
        exc_info=(arguments.exc_type, arguments.exc_value, arguments.exc_traceback),
        extra={"event_code": "runtime.unhandled_thread_exception"})


class _UsefulRuntimeAccessFilter(logging.Filter):
    """Keep mutations and failures in runtime logs without successful read noise."""

    _READ_METHODS = {"GET", "HEAD", "OPTIONS"}

    def filter(self, record: logging.LogRecord) -> bool:
        if get_diagnostics_preferences()["verbose_logging"] or record.name != "uvicorn.access":
            return True
        arguments = record.args
        if not isinstance(arguments, tuple) or len(arguments) < 5:
            return True
        try:
            method = str(arguments[1]).upper()
            status_code = int(arguments[4])
        except (TypeError, ValueError):
            return True
        return method not in self._READ_METHODS or status_code >= 400


def _prune_old_log_files(prefix: str, current_path: Path) -> None:
    """Keep dated logs bounded without reusing a generic filename."""
    try:
        existing = sorted(
            (
                path
                for path in LOG_DIR.glob(f"{prefix}-*.log*")
                if path.is_file() and path != current_path
            ),
            key=lambda path: path.stat().st_mtime_ns,
            reverse=True,
        )
    except OSError:
        return
    for stale_path in existing[max(0, LOG_RETENTION_FILES - 1):]:
        try:
            stale_path.unlink()
        except OSError:
            pass


class _SafeHandlerErrors:
    def handleError(self, record):
        # Stdlib error reports include the original, possibly secret arguments.
        try:
            sys.stderr.write("Keivotos: a diagnostic message could not be written.\n")
        except Exception:
            pass


class _SafeConsoleHandler(_SafeHandlerErrors, logging.StreamHandler):
    pass


class _SafeFileHandler(_SafeHandlerErrors, RotatingFileHandler):
    pass


def _file_handler(path: Path, formatter: logging.Formatter) -> RotatingFileHandler:
    handler = _SafeFileHandler(
        path,
        maxBytes=LOG_FILE_LIMIT_MB * 1024 * 1024,
        backupCount=LOG_ROLLOVER_FILES,
        encoding="utf-8",
    )
    handler.setFormatter(formatter)
    return handler



# Each writer owns only sanitized text, bounded by both record and byte counts.
class _Resilience:
    def resilience_setup(self, session, limits):
        self.session, self.limits = session, limits
        self.buffer = deque()
        self.buffer_bytes = self.dropped = self.recovered = 0
        self.fallback = None
        self._write_failed = False

    def guard_write(self, size):
        if self.fallback is not None:
            return False
        try:
            return self.session is not None and bool(guarded(Path(self.baseFilename), self.session.root)) and self.session.allow(size)
        except OSError:
            return False

    def _fallback_write(self, line):
        session = _get_fallback_session()
        target = session.directory / Path(self.baseFilename).name
        guarded(target, session.root)
        if not session.allow(len(line.encode("utf-8"))+1):
            raise OSError("Diagnostic budget unavailable")
        if self.fallback is None:
            self.fallback = RotatingFileHandler(target, maxBytes=self.limits["rotation_mib"]*MIB, backupCount=2,
                                                encoding="utf-8", delay=True)
            self.fallback.setFormatter(logging.Formatter("%(message)s"))
        prepared = logging.makeLogRecord({"msg": line, "args": (), "levelno": logging.INFO})
        if self.fallback.shouldRollover(prepared):
            self.fallback.doRollover()
        if self.fallback.stream is None:
            self.fallback.stream = self.fallback._open()
        self.fallback.stream.write(line+"\n");self.fallback.stream.flush()

    def handleError(self, record):
        self._write_failed = True
        try:
            if getattr(record, "_diagnostic_prepared", False):
                line = record.getMessage()
            elif isinstance(self, DiagnosticEventHandler):
                event, _ = self._event(record)
                line = json.dumps(event, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
            else:
                line = self.format(record)
            line = redact_log_text(line)
            size = len(line.encode("utf-8"))
            while self.buffer and (len(self.buffer)>=256 or self.buffer_bytes+size>MIB):
                old=self.buffer.popleft();self.buffer_bytes-=len(old.encode("utf-8"));self.dropped+=1
            if size<=MIB:
                self.buffer.append(line);self.buffer_bytes+=size
            else:
                self.dropped+=1
            self._drain()
            if size<=MIB and not self.buffer and self.fallback is not None:
                self._write_failed=False
        except Exception:
            self.dropped+=1
        path = self.fallback.baseFilename if self.fallback is not None else None
        bootstrap_notice("using_fallback" if not self.buffer else "storage_unavailable_buffered", path)

    def _drain(self):
        while self.buffer:
            line=self.buffer[0]
            try:
                self._fallback_write(line)
            except Exception:
                return
            self.buffer.popleft();self.buffer_bytes-=len(line.encode("utf-8"));self.recovered+=1
        if self.recovered:
            bootstrap_notice("buffer_recovered",self.fallback.baseFilename if self.fallback is not None else None,
                             counts={"retained":self.recovered,"dropped":self.dropped})

    def emit(self, record):
        if self.buffer:
            self._drain()
        self._write_failed=False
        if isinstance(self, DiagnosticEventHandler):
            super().emit(record)
        else:
            try:
                line=self.format(record)
                if not self.guard_write(len(line.encode("utf-8"))+1):
                    self.handleError(record)
                else:
                    super().emit(record)
            except Exception:
                self.handleError(record)

    def close(self):
        self.acquire()
        try:
            if hasattr(self,"buffer") and self.buffer:
                self._drain()
            if getattr(self,"fallback",None) is not None:
                self.fallback.close()
            super().close()
        finally:
            self.release()


class _ResilientFileHandler(_Resilience, _SafeFileHandler):
    pass


class _ResilientDiagnosticHandler(_Resilience, DiagnosticEventHandler):
    pass


class _DebugFilter(logging.Filter):
    def filter(self, record):
        if record.levelno>=logging.INFO:
            return True
        return record.name.startswith("keivotos") and debug_session()["active"]


_sessions = []
_fallback_session = None
_cleanup_result = {}


def _fallback_budget(limits):
    return min(16*MIB,limits["total_mib"]*MIB//4)


def _stream_paths(directory):
    return (directory / RUNTIME_LOG_FILE.name, directory / ACCESS_LOG_FILE.name,
            directory / "events-0001.jsonl")


def _get_fallback_session():
    global _fallback_session
    if _fallback_session is None:
        directory=fallback_root()/DIAGNOSTIC_RUN_ID
        _fallback_session=LogSession(directory.parent,DIAGNOSTIC_RUN_ID,_stream_paths(directory),
                                     get_log_policy(),budget_bytes=_fallback_budget(get_log_policy()))
        _sessions.append(_fallback_session)
        _fallback_session.cleanup()
    return _fallback_session


def diagnostics_state():
    writers=[handler for handler in _owned_handlers if isinstance(handler,_Resilience)]
    return {**bootstrap_state(), "buffered":sum(len(h.buffer) for h in writers),
            "dropped":sum(h.dropped for h in writers), "recovered":sum(h.recovered for h in writers),
            "fallback":any(h.fallback is not None or h.session is None or h.session is _fallback_session for h in writers),
            "cleanup":dict(_cleanup_result)}


def current_log_paths(configured=None):
    result=[]
    for path in configured or (RUNTIME_LOG_FILE,ACCESS_LOG_FILE):
        writer=next((h for h in _owned_handlers if isinstance(h,_Resilience) and getattr(h,"original_path",None)==path),None)
        result.append(Path(writer.fallback.baseFilename) if writer is not None and writer.fallback is not None
                      else Path(writer.baseFilename) if writer is not None else path)
    return tuple(result)


def set_debug_session(active):
    result=debug_session(active)
    logging.getLogger().setLevel(logging.DEBUG if result["active"] else logging.INFO)
    logging.getLogger("keivotos.diagnostics").info("Temporary debug session %s", "started" if active else "stopped",
        extra={"event_code":"diagnostics.debug_session","event_details":result})
    return result


def configure_runtime_logging() -> tuple[Path, Path]:
    """Configure once per run; repeated setup closes the previous owned writers."""
    with _logging_lock:
        return _configure_runtime_logging()


def _configure_runtime_logging() -> tuple[Path, Path]:
    global _fallback_session, _cleanup_result
    for handler in _owned_handlers:
        handler.close()
    _owned_handlers.clear()
    for session in _sessions:
        session.close()
    _sessions.clear();_fallback_session=None
    limits=get_log_policy()
    paths=(RUNTIME_LOG_FILE,ACCESS_LOG_FILE,DIAGNOSTIC_LOG_FILE)
    session=None
    try:
        session=LogSession(LOG_DIR,DIAGNOSTIC_RUN_ID,paths,limits,
                           budget_bytes=limits["total_mib"]*MIB-_fallback_budget(limits))
        _sessions.append(session)
        _cleanup_result=session.cleanup()
        for path in paths:
            guarded(path,session.root)
            with path.open("a",encoding="utf-8"):
                pass
    except (OSError,ValueError):
        bootstrap_notice("primary_log_directory_unavailable")
        try:
            session=_get_fallback_session()
            paths=_stream_paths(session.directory)
        except (OSError,ValueError):
            session=None
            bootstrap_notice("storage_unavailable_buffered")

    root_logger=logging.getLogger()
    root_logger.setLevel(logging.DEBUG if debug_session()["active"] else logging.INFO)
    formatter=_SafeFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s",datefmt="%Y-%m-%d %H:%M:%S")
    console=_SafeConsoleHandler()
    console.setFormatter(formatter);console.addFilter(_UsefulRuntimeAccessFilter());console.addFilter(_DebugFilter())
    runtime_handler=_ResilientFileHandler(paths[0],maxBytes=limits["rotation_mib"]*MIB,backupCount=2,encoding="utf-8",delay=True)
    runtime_handler.setFormatter(formatter);runtime_handler.resilience_setup(session,limits)
    runtime_handler.addFilter(_UsefulRuntimeAccessFilter())
    access_handler=_ResilientFileHandler(paths[1],maxBytes=limits["rotation_mib"]*MIB,backupCount=2,encoding="utf-8",delay=True)
    access_handler.setFormatter(formatter);access_handler.resilience_setup(session,limits)
    diagnostic_handler=_ResilientDiagnosticHandler(paths[2],DIAGNOSTIC_RUN_ID,
                            max_bytes=limits["rotation_mib"]*MIB,backups=2,delay=True)
    diagnostic_handler.resilience_setup(session,limits)
    diagnostic_handler.write_guard=diagnostic_handler.guard_write
    diagnostic_handler.addFilter(_UsefulRuntimeAccessFilter())
    for handler,original in zip((runtime_handler,access_handler,diagnostic_handler),(RUNTIME_LOG_FILE,ACCESS_LOG_FILE,DIAGNOSTIC_LOG_FILE)):
        handler.original_path=original
    _owned_handlers.extend((console,runtime_handler,access_handler,diagnostic_handler))
    for handler in (runtime_handler,access_handler,diagnostic_handler):
        handler.addFilter(_DebugFilter())
    root_logger.handlers.clear()
    for handler in (console,runtime_handler,diagnostic_handler):
        root_logger.addHandler(handler)
    access_logger=logging.getLogger("uvicorn.access")
    access_logger.handlers.clear();access_logger.setLevel(logging.INFO);access_logger.propagate=False
    for handler in _owned_handlers:
        access_logger.addHandler(handler)
    sys.excepthook=_unhandled_exception
    threading.excepthook=_unhandled_thread_exception
    return current_log_paths()


def debug_status():
    value=debug_session()
    if not value["active"] and logging.getLogger().level==logging.DEBUG:
        logging.getLogger().setLevel(logging.INFO)
    return value


def known_log_path(path):
    if not any(Path(path)==candidate for candidate in current_log_paths()):
        return False
    for session in _sessions:
        try:
            guarded(Path(path),session.root)
            return True
        except OSError:
            pass
    return False


def apply_log_policy():
    global _cleanup_result
    with _logging_lock:
        if not _owned_handlers:
            configure_runtime_logging()
            return
        limits=get_log_policy()
        for handler in _owned_handlers:
            if not isinstance(handler,_Resilience):
                continue
            handler.acquire()
            try:
                handler.limits=limits
                handler.maxBytes=limits["rotation_mib"]*MIB
                if handler.fallback is not None:
                    handler.fallback.maxBytes=handler.maxBytes
            finally:
                handler.release()
        for session in _sessions:
            with session._mutex:
                session.limits=limits
                session.budget_bytes=(_fallback_budget(limits) if session is _fallback_session else limits["total_mib"]*MIB-_fallback_budget(limits))
                _cleanup_result=session.cleanup()
